"""Testy spotového režimu (Bybit EU a spol.).

Spot se od perpetuálů liší ve třech zásadních věcech: nemá páku, nemá
poziční API, a neumí reduce-only stop příkazy — SL i TP musí vyhodnocovat
bot sám. Tyhle testy hlídají všechny tři.
"""
import pytest

from atb.config import AppConfig
from atb.models import Action, Side, Signal
from atb.state.store import Store
from atb.trader import Trader
from tests.conftest import FakeExchange


@pytest.fixture()
def spot_config() -> AppConfig:
    return AppConfig.model_validate({
        "mode": "paper", "dry_run": False, "database": ":memory:",
        "monitor": {"enabled": False}, "scanner": {"enabled": False, "auto_universe": False},
        "strategy": {"adaptive_learning": False},
        "exchange": {"id": "bybiteu", "account_type": "spot", "testnet": False},
    })


@pytest.fixture()
def spot_trader(spot_config) -> Trader:
    exchange = FakeExchange()
    exchange.tracks_positions = False
    exchange.can_short = False
    trader = Trader(spot_config, exchange=exchange, store=Store(":memory:"))
    yield trader
    trader.shutdown()


def buy(symbol: str = "BTC/USDT") -> Signal:
    return Signal(symbol=symbol, action=Action.ENTRY, side=Side.LONG,
                  timeframe="15m", confidence=0.9)


# ---------- konfigurace ----------

def test_spot_forces_no_leverage(spot_config):
    assert spot_config.risk.max_leverage == 1
    assert spot_config.risk.min_leverage == 1


def test_spot_disables_exchange_stops(spot_config):
    """Spotový účet reduce-only stopy neumí — musí je hlídat bot."""
    assert spot_config.exits.use_exchange_stops is False


def test_perpetual_config_keeps_leverage_and_exchange_stops():
    cfg = AppConfig()
    assert cfg.risk.max_leverage > 1
    assert cfg.exits.use_exchange_stops is True


# ---------- obchodování ----------

def test_spot_entry_opens_tracked_trade(spot_trader):
    result = spot_trader.handle_signal(buy())
    assert result["status"] == "executed", result
    assert result["plan"]["leverage"] == 1
    assert len(spot_trader.store.open_trades()) == 1


def test_short_is_rejected_on_spot(spot_trader):
    signal = buy()
    signal.side = Side.SHORT
    result = spot_trader.handle_signal(signal)
    assert result["status"] == "rejected"
    assert "short" in result["detail"].lower()


def test_no_stop_orders_are_sent_to_a_spot_exchange(spot_trader):
    spot_trader.handle_signal(buy())
    kinds = {o["type"] for o in spot_trader.exchange.orders}
    assert kinds == {"market"}, f"na spot se poslaly i {kinds - {'market'}}"


def test_open_symbols_come_from_records(spot_trader):
    spot_trader.handle_signal(buy())
    assert spot_trader.exchange.fetch_positions() == []      # burza o pozici neví
    assert spot_trader.open_symbols() == ["BTC/USDT"]        # bot ano


def test_status_shows_position_from_records(spot_trader):
    spot_trader.handle_signal(buy())
    status = spot_trader.status()
    assert len(status["open_positions"]) == 1
    assert status["open_positions"][0]["symbol"] == "BTC/USDT"


def test_duplicate_entry_still_blocked_on_spot(spot_trader):
    spot_trader.handle_signal(buy())
    second = spot_trader.handle_signal(buy())
    assert second["status"] == "rejected"


# ---------- lokální SL a TP ----------

def set_price(exchange: FakeExchange, price: float) -> None:
    last = list(exchange._ohlcv[-1])
    last[4] = price
    last[2] = max(last[2], price)
    last[3] = min(last[3], price)
    exchange._ohlcv[-1] = last


def test_local_stop_loss_closes_the_trade(spot_trader):
    spot_trader.handle_signal(buy())
    trade = spot_trader.store.open_trades()[0]

    set_price(spot_trader.exchange, trade["stop_loss"] * 0.999)
    spot_trader.positions.tick()

    assert spot_trader.store.open_trades() == []
    closed = spot_trader.store.recent_closed()[0]
    assert closed["exit_reason"] == "local_stop_loss"
    assert closed["pnl"] < 0


def test_local_take_profit_sells_only_part(spot_trader):
    spot_trader.handle_signal(buy())
    trade = spot_trader.store.open_trades()[0]
    first_tp = __import__("json").loads(trade["plan"])["take_profits"][0]

    set_price(spot_trader.exchange, first_tp["price"] * 1.001)
    spot_trader.positions.tick()

    still_open = spot_trader.store.open_trades()
    assert len(still_open) == 1, "TP1 nesmí zavřít celou pozici"
    assert still_open[0]["tp_filled"] == 1
    assert still_open[0]["qty_open"] < trade["quantity"]
    assert still_open[0]["realized"] > 0


def test_stop_wins_when_a_move_would_cross_both(spot_trader):
    """Konzervativní pravidlo: při nejednoznačnosti se počítá horší varianta."""
    spot_trader.handle_signal(buy())
    trade = spot_trader.store.open_trades()[0]
    set_price(spot_trader.exchange, trade["stop_loss"] * 0.99)
    spot_trader.positions.tick()
    assert spot_trader.store.recent_closed()[0]["exit_reason"] == "local_stop_loss"


def test_partial_profits_are_added_to_final_pnl(spot_trader):
    spot_trader.handle_signal(buy())
    trade = spot_trader.store.open_trades()[0]
    targets = __import__("json").loads(trade["plan"])["take_profits"]

    set_price(spot_trader.exchange, targets[0]["price"] * 1.001)
    spot_trader.positions.tick()
    realized_after_tp = spot_trader.store.open_trades()[0]["realized"]

    spot_trader.router.close("BTC/USDT", reason="test")
    closed = spot_trader.store.recent_closed()[0]
    assert closed["pnl"] > realized_after_tp * 0.9      # zisk z TP se nezahodil


def test_r_multiple_uses_the_original_stop(spot_trader):
    """Po posunu na breakeven nesmí R vyjít nesmyslně velké."""
    spot_trader.handle_signal(buy())
    trade = spot_trader.store.open_trades()[0]
    entry, stop = trade["entry"], trade["stop_loss"]

    set_price(spot_trader.exchange, entry + (entry - stop) * 1.2)
    spot_trader.positions.tick()                        # breakeven se aktivuje
    spot_trader.router.close("BTC/USDT", reason="test")

    closed = spot_trader.store.recent_closed()[0]
    assert closed["r_multiple"] is not None
    assert abs(closed["r_multiple"]) < 5


# ---------- skener na spotu ----------

def test_scanner_offers_no_shorts_on_spot(spot_trader):
    """Short v přehledu by lákal na obchod, který na spotu nejde provést."""
    spot_trader.cfg.scanner.watchlist = ["BTC/USDT"]
    entry = spot_trader.scanner.scan_symbol("BTC/USDT")
    assert set(entry["sides"]) == {"long"}
    assert all(t["side"] == "long" for t in entry["triggers"])

    spot_trader.scanner.scan_once()
    assert all(r["side"] == "long" for r in spot_trader.scanner.opportunities())


def test_scanner_still_offers_both_directions_on_perpetuals(config, exchange):
    config.strategy.adaptive_learning = False
    config.scanner.enabled = False
    config.scanner.auto_universe = False
    config.scanner.watchlist = ["BTC/USDT:USDT"]
    trader = Trader(config, exchange=exchange, store=Store(":memory:"))
    entry = trader.scanner.scan_symbol("BTC/USDT:USDT")
    assert set(entry["sides"]) == {"long", "short"}
    trader.shutdown()
