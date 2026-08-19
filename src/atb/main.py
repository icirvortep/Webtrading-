"""CLI bota.

Příklady:
    python -m atb run                      # spustí webhook server (paper režim)
    python -m atb venues                   # přehled burz s pákou
    python -m atb analyze BTC/USDT:USDT    # rozbor trhu bez obchodování
    python -m atb backtest BTC/USDT:USDT --timeframe 15m --limit 1500
    python -m atb status                   # equity, pozice, statistika
    python -m atb close-all                # nouzové uzavření všech pozic
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
from pathlib import Path

from .config import AppConfig, load_config
from .exchanges import registry
from .logging_setup import setup_logging
from .models import Side
from .trader import Trader

log = logging.getLogger("atb")


def _load_dotenv(path: str = ".env") -> None:
    """Minimální .env loader — bez další závislosti."""
    env_path = Path(path)
    if not env_path.exists():
        return
    import os

    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="atb", description="Adaptivní trading bot pro TradingView")
    parser.add_argument("--config", default=None, help="cesta ke config.yaml")
    parser.add_argument("--log-level", default=None, help="DEBUG/INFO/WARNING/ERROR")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="spustí webhook server a obchodní smyčku")
    run.add_argument("--host", default=None)
    run.add_argument("--port", type=int, default=None)
    run.add_argument("--live", action="store_true", help="přepne do živého režimu (vyžaduje potvrzení)")

    sub.add_parser("venues", help="přehled podporovaných burz a jejich páky")
    sub.add_parser("status", help="equity, otevřené pozice a statistika")
    sub.add_parser("close-all", help="uzavře všechny otevřené pozice")

    analyze = sub.add_parser("analyze", help="rozbor trhu (režim, skóre, návrh SL/TP)")
    analyze.add_argument("symbol")
    analyze.add_argument("--timeframe", default="15m")
    analyze.add_argument("--side", choices=["long", "short"], default="long")

    backtest = sub.add_parser("backtest", help="rychlý backtest nastavení strategie")
    backtest.add_argument("symbol")
    backtest.add_argument("--timeframe", default="15m")
    backtest.add_argument("--limit", type=int, default=1500)
    backtest.add_argument("--equity", type=float, default=10_000.0)

    test = sub.add_parser("test-signal", help="odešle testovací signál interně (bez HTTP)")
    test.add_argument("symbol")
    test.add_argument("--side", choices=["long", "short"], default="long")
    test.add_argument("--timeframe", default="15m")
    return parser


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    setup_logging(args.log_level or cfg.log_level, cfg.log_json, log_file="data/atb.log")

    handlers = {
        "venues": cmd_venues,
        "run": cmd_run,
        "status": cmd_status,
        "close-all": cmd_close_all,
        "analyze": cmd_analyze,
        "backtest": cmd_backtest,
        "test-signal": cmd_test_signal,
    }
    try:
        return handlers[args.command](cfg, args)
    except KeyboardInterrupt:
        print("\nPřerušeno uživatelem.")
        return 130
    except Exception as exc:
        name = type(exc).__name__
        log.debug("Detail chyby", exc_info=True)
        if "Network" in name or "Timeout" in name or "DNS" in name:
            print(f"Chyba sítě při komunikaci s burzou: {exc}")
            print("Zkontroluj připojení, firewall a jestli je burza v tvé zemi dostupná.")
        else:
            print(f"Chyba ({name}): {exc}")
        print("Podrobnosti: spusť znovu s --log-level DEBUG")
        return 1


# ---------- příkazy ----------

def cmd_venues(cfg: AppConfig, args: argparse.Namespace) -> int:
    print(registry.table())
    recommended = registry.recommend()
    print(f"\nAutomatické doporučení: {recommended.name} ({recommended.id}) — {recommended.notes}")
    print("Silné stránky: " + ", ".join(recommended.strengths))
    print(f"\nAktuálně nastaveno v configu: {cfg.exchange.id} (testnet={cfg.exchange.testnet})")
    return 0


def cmd_run(cfg: AppConfig, args: argparse.Namespace) -> int:
    import uvicorn

    from .webhook.server import create_app

    if args.live:
        cfg.mode = "live"
        cfg.dry_run = False
    if cfg.live and not _confirm_live(cfg):
        return 1

    trader = Trader(cfg)
    trader.start()
    app = create_app(cfg, trader)

    def _shutdown(signum, frame) -> None:
        log.info("Ukončuji (signál %s)…", signum)
        trader.shutdown(close_positions=False)
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    host = args.host or cfg.webhook.host
    port = args.port or cfg.webhook.port
    log.info("Webhook poslouchá na http://%s:%d%s", host, port, cfg.webhook.path)
    uvicorn.run(app, host=host, port=port, log_level=cfg.log_level.lower())
    return 0


def cmd_status(cfg: AppConfig, args: argparse.Namespace) -> int:
    trader = Trader(cfg)
    print(json.dumps(trader.status(), indent=2, ensure_ascii=False))
    trader.shutdown()
    return 0


def cmd_close_all(cfg: AppConfig, args: argparse.Namespace) -> int:
    trader = Trader(cfg)
    results = trader.router.close_all("cli")
    print(f"Uzavřeno pozic: {sum(1 for r in results if r.ok)} / {len(results)}")
    for result in results:
        if not result.ok:
            print(f"  chyba: {result.error}")
    trader.shutdown()
    return 0


def cmd_analyze(cfg: AppConfig, args: argparse.Namespace) -> int:
    from .strategy import exits

    trader = Trader(cfg)
    side = Side(args.side)
    snap, score = trader.engine.snapshot_for_side(args.symbol, args.timeframe, side)
    stop = exits.build_stop(side, snap.price, snap, cfg.exits)
    take_profits = exits.build_take_profits(side, snap.price, stop, snap, cfg.exits)

    print(f"\n=== {args.symbol} {args.timeframe} ({side.value}) ===")
    print(f"Cena:            {snap.price:.6f}")
    print(f"Režim:           {snap.regime.value} (síla trendu {snap.trend_strength:.2f})")
    print(f"ATR:             {snap.atr:.6f} ({snap.atr_pct:.2f} % ceny)")
    print(f"ADX / RSI:       {snap.adx:.1f} / {snap.rsi:.1f}")
    print(f"HTF trend:       {snap.htf_trend:+d}")
    print(f"Spread:          {snap.spread_bps:.2f} bps | funding {snap.funding_rate * 100:.4f} %")
    print(f"Skóre:           {score.value:.3f} (práh {cfg.strategy.min_score})"
          + (f"  VETO: {score.veto}" if score.veto else ""))
    print(f"Navržený SL:     {stop:.6f} ({abs(snap.price - stop) / snap.price * 100:.2f} %)")
    for index, tp in enumerate(take_profits, start=1):
        print(f"  TP{index}:           {tp.price:.6f} ({tp.r_multiple:.1f}R, {tp.fraction * 100:.0f} % pozice)")
    print("Důvody:          " + "; ".join(score.reasons))
    trader.shutdown()
    return 0


def cmd_backtest(cfg: AppConfig, args: argparse.Namespace) -> int:
    from .backtest import run_backtest

    trader = Trader(cfg)
    ohlcv = trader.exchange.fetch_ohlcv(args.symbol, args.timeframe, limit=args.limit)
    print(f"Načteno {len(ohlcv)} barů {args.symbol} {args.timeframe}")
    result = run_backtest(ohlcv, cfg, starting_equity=args.equity)
    print(f"\n=== Backtest {args.symbol} {args.timeframe} ===")
    print(result.summary())
    print("\nPozn.: backtest používá EMA cross jako náhradu za signál z TradingView,")
    print("neobsahuje funding ani proměnlivou likviditu. Slouží ke kontrole parametrů.")
    trader.shutdown()
    return 0


def cmd_test_signal(cfg: AppConfig, args: argparse.Namespace) -> int:
    from .models import Action, Signal

    trader = Trader(cfg)
    trader.start()
    signal_obj = Signal(
        symbol=args.symbol, action=Action.ENTRY, side=Side(args.side),
        timeframe=args.timeframe, strategy="cli-test", confidence=0.7,
    )
    result = trader.handle_signal(signal_obj)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    trader.shutdown()
    return 0


def _confirm_live(cfg: AppConfig) -> bool:
    print("\n" + "=" * 68)
    print("  ŽIVÝ REŽIM — bot bude obchodovat za reálné peníze")
    print("=" * 68)
    print(f"  Burza:             {cfg.exchange.id} (testnet={cfg.exchange.testnet})")
    print(f"  Riziko na obchod:  {cfg.risk.risk_per_trade_pct} % equity")
    print(f"  Max páka:          {cfg.risk.max_leverage}x")
    print(f"  Denní stop:        -{cfg.risk.max_daily_loss_pct} %")
    print(f"  Max pozic:         {cfg.risk.max_open_positions}")
    print("=" * 68)
    answer = input("Napiš 'OBCHODUJ ZIVE' pro potvrzení: ").strip()
    if answer != "OBCHODUJ ZIVE":
        print("Zrušeno — bot se nespustil.")
        return False
    return True


if __name__ == "__main__":
    raise SystemExit(main())
