"""Katalog burz s pákovým obchodováním perpetual futures.

`max_leverage` je *typický* strop pro hlavní páry u retail účtu. Reálný limit
závisí na velikosti pozice (risk tier), jurisdikci, verifikaci účtu a aktuálních
pravidlech burzy — bot si proto skutečný limit vždy ověřuje z metadat trhu
(`market_limits()`) a bere minimum z: burza / konfigurace / risk manager.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Venue:
    id: str                      # ccxt id
    name: str
    max_leverage: int            # typický strop pro BTC/ETH perp
    kind: str                    # "cex" | "dex"
    testnet: bool                # nabízí testnet/demo účet
    ccxt_type: str = "swap"
    notes: str = ""
    strengths: list[str] = field(default_factory=list)


VENUES: dict[str, Venue] = {
    "bybit": Venue(
        id="bybit", name="Bybit", max_leverage=100, kind="cex", testnet=True,
        notes="Nejlepší poměr likvidita/API/testnet. Doporučená výchozí volba.",
        strengths=["stabilní REST i WS API", "plnohodnotný testnet", "hluboká likvidita", "unified account"],
    ),
    "binanceusdm": Venue(
        id="binanceusdm", name="Binance USD-M Futures", max_leverage=125, kind="cex", testnet=True,
        notes="Nejvyšší likvidita na trhu; páka se stupňovitě snižuje s notionalem.",
        strengths=["nejužší spready", "nejvíc párů", "výborná dokumentace"],
    ),
    "bitget": Venue(
        id="bitget", name="Bitget", max_leverage=125, kind="cex", testnet=True,
        notes="Solidní API, nižší poplatky u nižších VIP úrovní.",
        strengths=["copy trading", "dobré poplatky", "demo účet"],
    ),
    "okx": Venue(
        id="okx", name="OKX", max_leverage=100, kind="cex", testnet=True,
        notes="Vyžaduje passphrase k API klíči.",
        strengths=["kvalitní matching engine", "demo trading", "portfolio margin"],
    ),
    "bingx": Venue(
        id="bingx", name="BingX", max_leverage=150, kind="cex", testnet=False,
        notes="Vyšší páka, ale mělčí knihy u exotických párů.",
        strengths=["páka až 150x", "jednoduché API"],
    ),
    "mexc": Venue(
        id="mexc", name="MEXC", max_leverage=200, kind="cex", testnet=False,
        notes="Nejvyšší nominální páka; u velkých pozic pozor na slippage.",
        strengths=["páka až 200x", "hodně altcoin perpů"],
    ),
    "kucoinfutures": Venue(
        id="kucoinfutures", name="KuCoin Futures", max_leverage=100, kind="cex", testnet=True,
        notes="Kontraktová velikost se liší od spotu — hlídat contract size.",
        strengths=["sandbox", "široká nabídka altcoinů"],
    ),
    "phemex": Venue(
        id="phemex", name="Phemex", max_leverage=100, kind="cex", testnet=True,
        strengths=["nízká latence", "testnet"],
    ),
    "gate": Venue(
        id="gate", name="Gate.io", max_leverage=100, kind="cex", testnet=True,
        strengths=["velmi široká nabídka perpů"],
    ),
    "hyperliquid": Venue(
        id="hyperliquid", name="Hyperliquid", max_leverage=50, kind="dex", testnet=True,
        notes="On-chain perp DEX; podpis transakcí peněženkou místo API klíče.",
        strengths=["bez KYC", "transparentní on-chain book", "testnet"],
    ),
}

#: Pořadí doporučení, pokud si uživatel nevybere sám.
RECOMMENDED_ORDER = ["bybit", "binanceusdm", "bitget", "okx", "kucoinfutures", "bingx", "mexc"]


def get(venue_id: str) -> Venue | None:
    return VENUES.get(venue_id.lower())


def max_leverage(venue_id: str, fallback: int = 20) -> int:
    venue = get(venue_id)
    return venue.max_leverage if venue else fallback


def recommend(require_testnet: bool = True, min_leverage: int = 50) -> Venue:
    """Automatický výběr burzy: preferuje testnet a dostatečnou páku."""
    for vid in RECOMMENDED_ORDER:
        venue = VENUES[vid]
        if venue.max_leverage < min_leverage:
            continue
        if require_testnet and not venue.testnet:
            continue
        return venue
    return VENUES["bybit"]


def table() -> str:
    """Přehled burz pro CLI."""
    header = f"{'ID':<16}{'Burza':<24}{'Max páka':>9}  {'Testnet':<8}{'Typ':<5}"
    lines = [header, "-" * len(header)]
    for venue in VENUES.values():
        lines.append(
            f"{venue.id:<16}{venue.name:<24}{venue.max_leverage:>7}x  "
            f"{'ano' if venue.testnet else 'ne':<8}{venue.kind:<5}"
        )
    lines.append("")
    lines.append("Páka je orientační (mění se dle risk tier, jurisdikce a pravidel burzy).")
    return "\n".join(lines)
