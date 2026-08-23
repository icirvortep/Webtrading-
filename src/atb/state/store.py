"""Perzistence stavu v SQLite: signály, obchody, denní statistika, učení podle režimu.

Databáze je jediný zdroj pravdy pro risk limity (denní ztráta, cooldowny)
a pro adaptaci strategie — po restartu bot navazuje tam, kde skončil.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL,
    side TEXT,
    accepted INTEGER NOT NULL,
    reason TEXT,
    score REAL,
    regime TEXT,
    payload TEXT
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id TEXT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    regime TEXT,
    opened_at REAL NOT NULL,
    closed_at REAL,
    entry REAL NOT NULL,
    exit REAL,
    quantity REAL NOT NULL,
    leverage INTEGER,
    stop_loss REAL,
    risk_amount REAL,
    pnl REAL,
    r_multiple REAL,
    fees REAL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'open',
    plan TEXT,
    exit_reason TEXT
);
CREATE TABLE IF NOT EXISTS equity_curve (
    ts REAL PRIMARY KEY,
    equity REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol, status);
CREATE INDEX IF NOT EXISTS idx_trades_closed ON trades(closed_at);
CREATE INDEX IF NOT EXISTS idx_trades_regime ON trades(regime, status);
"""


def _utc_day(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), tz=UTC).strftime("%Y-%m-%d")


def _day_start(ts: float | None = None) -> float:
    now = datetime.fromtimestamp(ts or time.time(), tz=UTC)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


class Store:
    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------- signály ----------

    def record_signal(
        self, signal_id: str, symbol: str, action: str, side: str | None,
        accepted: bool, reason: str = "", score: float | None = None,
        regime: str | None = None, payload: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO signals "
                "(id, ts, symbol, action, side, accepted, reason, score, regime, payload) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (signal_id, time.time(), symbol, action, side, int(accepted), reason,
                 score, regime, json.dumps(payload or {}, default=str)),
            )
            self._conn.commit()

    def recent_signals(self, limit: int = 30) -> list[dict[str, Any]]:
        """Poslední rozhodnutí bota — přijaté i zamítnuté, s důvodem."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, ts, symbol, action, side, accepted, reason, score, regime "
                "FROM signals ORDER BY ts DESC LIMIT ?", (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def seen_signal(self, signal_id: str) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM signals WHERE id = ?", (signal_id,)).fetchone()
        return row is not None

    # ---------- obchody ----------

    def open_trade(self, plan: dict[str, Any], entry_price: float, quantity: float) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO trades (signal_id, symbol, side, regime, opened_at, entry, "
                "quantity, leverage, stop_loss, risk_amount, status, plan) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,'open',?)",
                (plan.get("signal_id"), plan["symbol"], plan["side"], plan.get("regime"),
                 time.time(), entry_price, quantity, plan.get("leverage"),
                 plan.get("stop_loss"), plan.get("risk_amount"), json.dumps(plan, default=str)),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def close_trade(
        self, trade_id: int, exit_price: float, pnl: float,
        fees: float = 0.0, exit_reason: str = "",
    ) -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT entry, stop_loss, quantity FROM trades WHERE id = ?", (trade_id,)
            ).fetchone()
            r_multiple = None
            if row and row["stop_loss"]:
                risk = abs(row["entry"] - row["stop_loss"]) * row["quantity"]
                if risk > 0:
                    r_multiple = round(pnl / risk, 4)
            self._conn.execute(
                "UPDATE trades SET closed_at=?, exit=?, pnl=?, fees=?, r_multiple=?, "
                "status='closed', exit_reason=? WHERE id=?",
                (time.time(), exit_price, pnl, fees, r_multiple, exit_reason, trade_id),
            )
            self._conn.commit()

    def update_trade_stop(self, trade_id: int, stop_loss: float) -> None:
        with self._lock:
            self._conn.execute("UPDATE trades SET stop_loss=? WHERE id=?", (stop_loss, trade_id))
            self._conn.commit()

    def open_trades(self, symbol: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM trades WHERE status='open'"
        args: tuple = ()
        if symbol:
            query += " AND symbol=?"
            args = (symbol,)
        with self._lock:
            rows = self._conn.execute(query, args).fetchall()
        return [dict(r) for r in rows]

    def recent_closed(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM trades WHERE status='closed' ORDER BY closed_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- denní limity a cooldowny ----------

    def daily_pnl(self) -> float:
        start = _day_start()
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(pnl), 0) AS total FROM trades "
                "WHERE status='closed' AND closed_at >= ?", (start,),
            ).fetchone()
        return float(row["total"] or 0.0)

    def daily_trade_count(self) -> int:
        start = _day_start()
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM trades WHERE opened_at >= ?", (start,)
            ).fetchone()
        return int(row["n"] or 0)

    def last_loss_time(self, symbol: str | None = None) -> float | None:
        query = "SELECT closed_at FROM trades WHERE status='closed' AND pnl < 0"
        args: tuple = ()
        if symbol:
            query += " AND symbol=?"
            args = (symbol,)
        query += " ORDER BY closed_at DESC LIMIT 1"
        with self._lock:
            row = self._conn.execute(query, args).fetchone()
        return float(row["closed_at"]) if row and row["closed_at"] else None

    def loss_streak(self) -> int:
        with self._lock:
            rows = self._conn.execute(
                "SELECT pnl FROM trades WHERE status='closed' ORDER BY closed_at DESC LIMIT 20"
            ).fetchall()
        streak = 0
        for row in rows:
            if (row["pnl"] or 0.0) < 0:
                streak += 1
            else:
                break
        return streak

    def open_risk_total(self) -> float:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(risk_amount), 0) AS total FROM trades WHERE status='open'"
            ).fetchone()
        return float(row["total"] or 0.0)

    # ---------- adaptace podle režimu ----------

    def regime_stats(self, regime: str, lookback: int = 60) -> dict[str, float]:
        """Expektance a win rate posledních N uzavřených obchodů v daném režimu."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT pnl, r_multiple FROM trades WHERE status='closed' AND regime=? "
                "ORDER BY closed_at DESC LIMIT ?", (regime, lookback),
            ).fetchall()
        if not rows:
            return {"trades": 0, "win_rate": 0.0, "expectancy_r": 0.0, "avg_pnl": 0.0}
        pnls = [float(r["pnl"] or 0.0) for r in rows]
        r_values = [float(r["r_multiple"]) for r in rows if r["r_multiple"] is not None]
        wins = sum(1 for p in pnls if p > 0)
        return {
            "trades": len(rows),
            "win_rate": round(wins / len(rows), 4),
            "expectancy_r": round(sum(r_values) / len(r_values), 4) if r_values else 0.0,
            "avg_pnl": round(sum(pnls) / len(pnls), 6),
        }

    # ---------- equity a kv ----------

    def record_equity(self, equity: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO equity_curve (ts, equity) VALUES (?, ?)",
                (round(time.time(), 3), equity),
            )
            self._conn.commit()

    def start_of_day_equity(self, current: float) -> float:
        """Equity na začátku UTC dne — základ pro limit denní ztráty."""
        key = f"sod_equity:{_utc_day()}"
        stored = self.get(key)
        if stored is None:
            self.set(key, str(current))
            return current
        return float(stored)

    def get(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, value)
            )
            self._conn.commit()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(pnl),0) pnl, "
                "COALESCE(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END),0) wins "
                "FROM trades WHERE status='closed'"
            ).fetchone()
        total = int(row["n"] or 0)
        return {
            "closed_trades": total,
            "total_pnl": round(float(row["pnl"] or 0.0), 6),
            "win_rate": round(float(row["wins"] or 0) / total, 4) if total else 0.0,
            "open_trades": len(self.open_trades()),
            "daily_pnl": round(self.daily_pnl(), 6),
        }
