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
import threading
import webbrowser
from pathlib import Path

from .config import AppConfig, load_config
from .exchanges import registry
from .logging_setup import setup_logging
from .models import Side
from .trader import Trader

log = logging.getLogger("atb")


def _load_dotenv(path: str = ".env") -> None:
    """Minimální .env loader — bez další závislosti.

    Zvládá `KLIC=hodnota`, uvozovky i komentář na konci řádku
    (`ATB_MODE=paper   # paper | live`). Hodnota v uvozovkách si `#` ponechá,
    aby šla použít třeba v hesle.
    """
    env_path = Path(path)
    if not env_path.exists():
        return
    import os

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]                      # uvozovky = hodnota doslova
        else:
            value = value.split(" #", 1)[0].split("\t#", 1)[0].strip()
        os.environ.setdefault(key.strip(), value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="atb", description="Adaptivní trading bot pro TradingView")
    parser.add_argument("--config", default=None, help="cesta ke config.yaml")
    parser.add_argument("--log-level", default=None, help="DEBUG/INFO/WARNING/ERROR")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="spustí webhook server a obchodní smyčku")
    run.add_argument("--host", default=None)
    run.add_argument("--port", type=int, default=None)
    run.add_argument("--live", action="store_true", help="přepne do živého režimu (vyžaduje potvrzení)")
    run.add_argument("--offline", action="store_true",
                     help="simulovaná data — vyzkoušení rozhraní bez klíčů a bez internetu")
    run.add_argument("--no-browser", action="store_true", help="neotvírat prohlížeč")

    demo = sub.add_parser("demo", help="ukázka celého toku offline — bez klíčů a bez internetu")
    demo.add_argument("--symbol", default="BTC/USDT:USDT")
    demo.add_argument("--side", choices=["long", "short"], default="long")
    demo.add_argument("--equity", type=float, default=10_000.0)
    demo.add_argument("--timeframe", default="15m")

    preflight = sub.add_parser(
        "preflight", help="prověří živé napojení na burzu dřív, než se začne obchodovat")
    preflight.add_argument("--symbol", default=None, help="na kterém trhu zkoušet (jinak nejlepší z žebříčku)")
    preflight.add_argument("--live-order", action="store_true",
                           help="opravdu poslat nejmenší možný příkaz a hned ho zavřít")

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
        "demo": cmd_demo,
        "preflight": cmd_preflight,
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

def cmd_preflight(cfg: AppConfig, args: argparse.Namespace) -> int:
    """Projde vše, co musí fungovat, než bot smí obchodovat za reálné peníze.

    Smysl je odhalit potíže na klidném místě — chybějící oprávnění klíče, jiný
    formát symbolu, minimum burzy — místo aby se ukázaly uprostřed obchodu.
    """
    from .exchanges.ccxt_adapter import CCXTExchange, ExchangeError

    checks: list[tuple[bool, str]] = []

    def check(ok: bool, label: str, detail: str = "") -> bool:
        checks.append((ok, label))
        mark = "✓" if ok else "✗"
        print(f"  {mark} {label}" + (f"\n      {detail}" if detail else ""))
        return ok

    print("\n" + "=" * 66)
    print(f"  KONTROLA ŽIVÉHO NAPOJENÍ — {cfg.exchange.id} ({cfg.exchange.account_type})")
    print("=" * 66)

    creds = cfg.exchange.credentials()
    if not check(bool(creds.get("apiKey") and creds.get("secret")), "API klíče v prostředí",
                 f"chybí {cfg.exchange.api_key_env} nebo {cfg.exchange.api_secret_env} v .env"):
        return 1

    try:
        exchange = CCXTExchange(cfg.exchange)
    except ExchangeError as exc:
        check(False, "typ trhu podporován burzou", str(exc))
        return 1
    check(True, f"typ trhu '{cfg.exchange.account_type}' burza podporuje")

    try:
        exchange.load_markets()
        symbols = exchange.list_symbols(cfg.exchange.quote)
    except Exception as exc:
        check(False, "načtení seznamu trhů", str(exc))
        return 1
    if not check(bool(symbols), "seznam trhů", f"nalezeno {len(symbols)} trhů v {cfg.exchange.quote}"):
        return 1
    print(f"      příklad: {', '.join(symbols[:4])}")

    try:
        balance = exchange.fetch_balance()
    except Exception as exc:
        check(False, "čtení zůstatku (oprávnění klíče)",
              f"{exc}\n      Klíč nejspíš nemá právo číst účet, nebo je vázaný na jinou IP.")
        return 1
    check(True, "čtení zůstatku — klíč funguje",
          f"{balance.equity:.2f} {balance.currency} (volné {balance.free:.2f})")

    symbol = args.symbol or (symbols[0] if symbols else None)
    limits = exchange.market_limits(symbol)
    price = float(exchange.fetch_ticker(symbol).get("last") or 0.0)
    check(price > 0, f"tržní data pro {symbol}", f"cena {price:.6f}")

    min_cost = max(limits.get("min_cost", 0.0), cfg.risk.min_notional_usd)
    risk_amount = balance.equity * cfg.risk.risk_per_trade_pct / 100.0
    print(f"\n  Při riziku {cfg.risk.risk_per_trade_pct} % vsadíš {risk_amount:.2f} "
          f"{balance.currency} na obchod.")
    print(f"  Minimum burzy pro {symbol}: {min_cost:.2f} {balance.currency}.")

    # Se stopem kolem 2 % je hodnota pozice zhruba padesátinásobek rizika.
    typical_notional = risk_amount * 50
    check(typical_notional >= min_cost, "velikost obchodu nad minimem burzy",
          f"typická pozice ~{typical_notional:.2f} {balance.currency}"
          + ("" if typical_notional >= min_cost else
             " — navyš vklad, nebo zvedni risk_per_trade_pct"))

    if args.live_order:
        print("\n  --- ZKUŠEBNÍ PŘÍKAZ ZA REÁLNÉ PENÍZE ---")
        quantity = exchange.amount_to_precision(symbol, (min_cost * 1.1) / price)
        answer = input(f"  Koupit {quantity} {symbol} (~{quantity * price:.2f} "
                       f"{balance.currency}) a hned prodat? [ano/ne]: ").strip().lower()
        if answer != "ano":
            print("  Přeskočeno.")
        else:
            from .models import Side

            bought = exchange.create_market_order(symbol, Side.LONG, quantity)
            if not check(bought.ok, "nákup prošel", bought.error or
                         f"plnění {bought.filled_qty} @ {bought.filled_price}"):
                return 1
            sold = exchange.create_market_order(symbol, Side.SHORT, bought.filled_qty or quantity)
            check(sold.ok, "prodej prošel", sold.error or
                  f"plnění {sold.filled_qty} @ {sold.filled_price}")
            print("      (rozdíl proti vkladu jsou poplatky za obě strany)")

    failed = [label for ok, label in checks if not ok]
    print("\n" + "=" * 66)
    if failed:
        print(f"  NEPROŠLO: {len(failed)} z {len(checks)} — {', '.join(failed)}")
        print("=" * 66 + "\n")
        return 1
    print(f"  Vše prošlo ({len(checks)} kontrol).")
    if not args.live_order:
        print("  Poslední jistota: `atb preflight --live-order` pošle jeden")
        print("  nejmenší možný reálný příkaz a hned ho zavře.")
    print("=" * 66 + "\n")
    return 0


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

    if args.offline:
        cfg.mode = "offline"
        cfg.dry_run = False
    if args.live:
        cfg.mode = "live"
        cfg.dry_run = False
    if cfg.live and not _confirm_live(cfg):
        return 1

    trader = Trader(cfg)
    trader.start()
    app = create_app(cfg, trader, config_path=args.config)

    def _shutdown(signum, frame) -> None:
        log.info("Ukončuji (signál %s)…", signum)
        trader.shutdown(close_positions=False)
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    host = args.host or cfg.webhook.host
    port = args.port or cfg.webhook.port
    if cfg.ui.bind_localhost_only and host == "0.0.0.0":
        # rozhraní nemá přihlašování — ven ho pouštíme jen na výslovné přání
        host = "127.0.0.1"

    url = f"http://localhost:{port}/"
    print("\n" + "=" * 60)
    print(f"  Rozhraní běží na:  {url}")
    print(f"  Webhook:           http://{host}:{port}{cfg.webhook.path}")
    print(f"  Režim:             {cfg.mode.upper()}"
          + ("  (simulovaná data)" if cfg.mode == "offline" else ""))
    print("  Ukončíš klávesami: Ctrl+C")
    print("=" * 60 + "\n")

    if cfg.ui.enabled and not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

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


def cmd_demo(cfg: AppConfig, args: argparse.Namespace) -> int:
    """Projde celý řetězec na vygenerovaných datech — nic se nikam neodesílá."""
    from .exchanges.offline import OfflineExchange
    from .models import Action, Signal
    from .state.store import Store

    cfg.mode = "paper"
    cfg.dry_run = False
    cfg.monitor.enabled = False
    exchange = OfflineExchange(cfg.exchange, equity=args.equity)
    trader = Trader(cfg, exchange=exchange, store=Store(":memory:"))

    print("\n" + "=" * 72)
    print("  UKÁZKA — simulovaná data, žádné API klíče, žádné reálné objednávky")
    print("=" * 72)

    balance = exchange.fetch_balance()
    side = Side(args.side)
    print(f"\n[1/5] Účet: {balance.equity:.2f} {balance.currency}, "
          f"riziko na obchod {cfg.risk.risk_per_trade_pct} %")

    snap = trader.engine.analyze(args.symbol, args.timeframe)
    print(f"\n[2/5] Rozbor trhu {args.symbol} {args.timeframe}")
    print(f"      cena {snap.price:.2f} | režim {snap.regime.value} "
          f"| ATR {snap.atr_pct:.2f} % | ADX {snap.adx:.1f} "
          f"| RSI {snap.rsi:.1f} | HTF {snap.htf_trend:+d}")

    print(f"\n[3/5] Přichází signál z TradingView: {side.value.upper()} {args.symbol}")
    result = trader.handle_signal(Signal(
        symbol=args.symbol, action=Action.ENTRY, side=side,
        timeframe=args.timeframe, strategy="demo", confidence=0.8,
    ))

    if result["status"] != "executed":
        print(f"      → Signál ZAMÍTNUT ({result.get('reason')}): {result.get('detail')}")
        print("\n      Právě tohle je hlavní práce bota — většinu signálů odfiltruje.")
        print("      Zkus druhý směr: python -m atb demo --side "
              f"{'short' if side is Side.LONG else 'long'}")
        trader.shutdown()
        return 0

    plan = result["plan"]
    r = abs(plan["entry"] - plan["stop_loss"])
    print(f"      → PŘIJAT (skóre {plan['score']:.3f})")
    print("\n[4/5] Plán obchodu")
    print(f"      vstup      {plan['entry']:.2f}")
    print(f"      stop loss  {plan['stop_loss']:.2f}  ({r / plan['entry'] * 100:.2f} % od vstupu)")
    for index, tp in enumerate(plan["take_profits"], start=1):
        print(f"      TP{index}        {tp['price']:.2f}  ({tp['r']:.1f}R, "
              f"{tp['fraction'] * 100:.0f} % pozice)")
    print(f"      množství   {plan['quantity']:.6f}  (notional {plan['notional']:.2f} USDT)")
    print(f"      páka       {plan['leverage']}x")
    print(f"      riziko     {plan['risk_amount']:.2f} USDT = "
          f"{plan['risk_pct']:.2f} % účtu  ← zásah SL stojí přesně tolik")
    print(f"      kontrola   {r:.2f} × {plan['quantity']:.6f} = "
          f"{r * plan['quantity']:.2f} USDT")

    print("\n      Důvody rozhodnutí:")
    for reason in plan["reasons"]:
        print(f"        · {reason}")

    print("\n[5/5] Řízení pozice v čase (cena roste o 1.2R a pak o 8R)")
    trade = trader.store.open_trades()[0]
    entry, stop = trade["entry"], trade["stop_loss"]
    for label, multiple in (("+1.2R", 1.2), ("+8R", 8.0)):
        _shift_offline_price(exchange, args.symbol, entry + (entry - stop) * multiple * side.sign)
        trader.positions.tick()
        current = trader.store.open_trades()[0]["stop_loss"]
        moved = "breakeven" if abs(current - entry) < entry * 0.002 else "trailing"
        print(f"      cena {label:>5} → SL posunut na {current:.2f} ({moved})")

    print("\n" + "=" * 72)
    print("  Takhle to poběží i naostro — jen s reálnými daty a klíči.")
    print("  Další krok:  python -m atb run     (webhook server, paper režim)")
    print("=" * 72 + "\n")
    trader.shutdown()
    return 0


def _shift_offline_price(exchange, symbol: str, price: float) -> None:
    """Posune poslední svíčku offline burzy na zadanou cenu."""
    for key, series in exchange._series.items():
        if not key.startswith(symbol):
            continue
        last = list(series[-1])
        last[4] = price
        last[2] = max(last[2], price)
        last[3] = min(last[3], price)
        series[-1] = last


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
