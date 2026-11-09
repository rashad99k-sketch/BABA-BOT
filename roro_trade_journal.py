"""SQLite trade journal with atomic snapshots for restart recovery."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import json
import sqlite3
import threading
import time
from typing import Any, Dict, Iterable, Optional


class TradeJournal:
    def __init__(self, path: str):
        self.path = str(Path(path))
        self._lock = threading.RLock()
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _db(self):
        with self._lock:
            db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            try:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA synchronous=FULL")
                db.execute("PRAGMA busy_timeout=5000")
                yield db
            finally:
                db.close()

    def _init_schema(self) -> None:
        with self._db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                trade_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, side TEXT NOT NULL,
                setup_type TEXT, status TEXT NOT NULL, entry_ts REAL, entry_qty REAL,
                entry_price REAL, sl_price REAL, native_sl_order_id TEXT,
                tp1_hit INTEGER NOT NULL DEFAULT 0, tp2_hit INTEGER NOT NULL DEFAULT 0,
                remaining_qty REAL, close_ts REAL, close_price REAL, close_reason TEXT,
                close_confidence TEXT, realized_pnl REAL, finalized INTEGER NOT NULL DEFAULT 0,
                actual_filled_qty REAL, tp1_filled_qty REAL DEFAULT 0, tp1_execution_price REAL,
                tp2_filled_qty REAL DEFAULT 0, trailing_state TEXT, highest_favorable REAL,
                lowest_favorable REAL, mfe REAL, profit_mode TEXT, exchange_state TEXT,
                pending_orders TEXT, last_reconciled_ts REAL, lifecycle_state TEXT,
                state_json TEXT, updated_ts REAL, client_order_id TEXT, exchange_order_id TEXT,
                position_mode TEXT, position_side TEXT, leverage REAL, requested_qty REAL,
                average_fill_price REAL, actual_entry_price REAL, initial_sl REAL,
                recovery_status TEXT, close_evidence TEXT, event_ids TEXT,
                tp1_price REAL, tp2_price REAL, native_sl_verified INTEGER NOT NULL DEFAULT 0,
                native_sl_status TEXT
            );
            CREATE TABLE IF NOT EXISTS trade_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, trade_id TEXT NOT NULL,
                event_key TEXT NOT NULL, event_type TEXT NOT NULL, payload TEXT,
                created_ts REAL NOT NULL, UNIQUE(trade_id, event_key)
            );
            CREATE INDEX IF NOT EXISTS ix_trade_events_trade ON trade_events(trade_id);
            CREATE TABLE IF NOT EXISTS consumed_events (
                event_id TEXT PRIMARY KEY, trade_id TEXT NOT NULL, created_ts REAL NOT NULL
            );
            """)
            # Upgrade databases created by earlier releases without discarding rows.
            existing = {row[1] for row in db.execute("PRAGMA table_info(trades)")}
            migrations = {
                "actual_filled_qty": "REAL", "tp1_filled_qty": "REAL DEFAULT 0",
                "tp1_execution_price": "REAL", "tp2_filled_qty": "REAL DEFAULT 0",
                "trailing_state": "TEXT", "highest_favorable": "REAL", "lowest_favorable": "REAL",
                "mfe": "REAL", "profit_mode": "TEXT", "exchange_state": "TEXT",
                "pending_orders": "TEXT", "last_reconciled_ts": "REAL", "lifecycle_state": "TEXT",
                "state_json": "TEXT", "updated_ts": "REAL", "client_order_id": "TEXT",
                "exchange_order_id": "TEXT", "position_mode": "TEXT", "position_side": "TEXT",
                "leverage": "REAL", "requested_qty": "REAL", "average_fill_price": "REAL",
                "actual_entry_price": "REAL", "initial_sl": "REAL", "recovery_status": "TEXT",
                "close_evidence": "TEXT", "event_ids": "TEXT",
                "tp1_price": "REAL", "tp2_price": "REAL",
                "native_sl_verified": "INTEGER NOT NULL DEFAULT 0", "native_sl_status": "TEXT",
            }
            for name, ddl in migrations.items():
                if name not in existing:
                    db.execute(f"ALTER TABLE trades ADD COLUMN {name} {ddl}")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_trades_client_order_id ON trades(client_order_id) WHERE client_order_id IS NOT NULL")

    def persist_entry_pending(self, *, trade_id: str, client_order_id: str, symbol: str,
                              side: str, position_mode: str, position_side: str,
                              leverage: float, requested_qty: float, entry_ts: float,
                              status: str = "ENTRY_PENDING", **extra: Any) -> None:
        """Commit identity and planned management state before any exchange create_order call."""
        management_state = extra.get("management_state")
        state_json = json.dumps(management_state, sort_keys=True, default=str, separators=(",", ":")) if isinstance(management_state, dict) else None
        initial_sl = extra.get("structural_stop")
        tp1_price = management_state.get("tp1_price") if isinstance(management_state, dict) else None
        tp2_price = management_state.get("tp2_price") if isinstance(management_state, dict) else None
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""INSERT INTO trades(
                trade_id,symbol,side,status,entry_ts,entry_qty,remaining_qty,actual_filled_qty,
                client_order_id,position_mode,position_side,leverage,requested_qty,recovery_status,
                sl_price,initial_sl,state_json,updated_ts,tp1_price,tp2_price)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(trade_id) DO UPDATE SET status=excluded.status,
                client_order_id=excluded.client_order_id,position_mode=excluded.position_mode,
                position_side=excluded.position_side,leverage=excluded.leverage,
                requested_qty=excluded.requested_qty,sl_price=COALESCE(excluded.sl_price,trades.sl_price),
                initial_sl=COALESCE(excluded.initial_sl,trades.initial_sl),
                tp1_price=COALESCE(excluded.tp1_price,trades.tp1_price),
                tp2_price=COALESCE(excluded.tp2_price,trades.tp2_price),
                state_json=COALESCE(excluded.state_json,trades.state_json),updated_ts=excluded.updated_ts""",
                (str(trade_id), str(symbol), str(side), str(status), float(entry_ts),
                 float(requested_qty), 0.0, 0.0, str(client_order_id), str(position_mode),
                 str(position_side), float(leverage), float(requested_qty), "ENTRY_PENDING",
                 initial_sl, initial_sl, state_json, time.time(), tp1_price, tp2_price))
            self.event(str(trade_id), f"entry-pending:{client_order_id}", "ENTRY_PENDING",
                       {"client_order_id": str(client_order_id), "symbol": str(symbol),
                        "requested_qty": float(requested_qty), "structural_stop": initial_sl,
                        "tp1_price": tp1_price, "tp2_price": tp2_price,
                        "management_state_persisted": bool(state_json)}, db=db)
            db.execute("COMMIT")

    def record_execution(self, trade_id: str, *, exchange_order_id: Optional[str],
                         filled_qty: float, remaining_qty: float,
                         average_fill_price: Optional[float], actual_entry_price: Optional[float],
                         status: str, recovery_status: str = "") -> None:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state_json,entry_qty,entry_price FROM trades WHERE trade_id=?",
                             (str(trade_id),)).fetchone()
            state = {}
            if row and row[0]:
                try:
                    parsed = json.loads(row[0])
                    state = parsed if isinstance(parsed, dict) else {}
                except (TypeError, ValueError):
                    state = {}
            filled = max(0.0, float(filled_qty or 0.0))
            remaining = max(0.0, float(remaining_qty or 0.0))
            entry = actual_entry_price if actual_entry_price is not None else average_fill_price
            if filled > 0:
                # The actual executed amount is the original position size. The
                # order's unfilled remainder is a different quantity and must not
                # overwrite the still-open exchange exposure in the trade journal.
                state.update({"actual_filled_qty": filled, "initial_qty": filled,
                              "qty": filled, "remaining_qty": filled,
                              "exchange_qty": filled, "order_remaining_qty": remaining})
            else:
                state["order_remaining_qty"] = remaining
            if entry is not None and float(entry) > 0:
                state["entry"] = float(entry)
            state.update({"trade_id": str(trade_id), "entry_execution_state": str(status),
                          "exchange_order_id": exchange_order_id})
            db.execute("""UPDATE trades SET exchange_order_id=?, actual_filled_qty=CASE WHEN ?>0 THEN ? ELSE actual_filled_qty END,
                entry_qty=CASE WHEN ?>0 THEN ? ELSE entry_qty END,
                remaining_qty=CASE WHEN ?>0 THEN ? ELSE remaining_qty END,
                average_fill_price=?, actual_entry_price=?,
                entry_price=COALESCE(?,entry_price), status=?, recovery_status=?,
                state_json=?, updated_ts=? WHERE trade_id=?""",
                (exchange_order_id, filled, filled, filled, filled, filled, filled, average_fill_price,
                 actual_entry_price, entry, str(status), str(recovery_status),
                 json.dumps(state, sort_keys=True, default=str, separators=(",", ":")),
                 time.time(), str(trade_id)))
            self.event(str(trade_id), f"execution:{status}:{exchange_order_id or 'unknown'}",
                       "EXECUTION_VERIFIED", {"status": str(status), "filled_qty": filled,
                                               "remaining_qty": remaining,
                                               "average_fill_price": average_fill_price}, db=db)
            db.execute("COMMIT")

    def start_trade(self, trade_id: str, symbol: str, side: str, setup_type: str,
                    qty: float, entry_price: float, sl_price: Optional[float] = None) -> None:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""INSERT INTO trades(
                trade_id,symbol,side,setup_type,status,entry_ts,entry_qty,entry_price,
                sl_price,remaining_qty,actual_filled_qty,updated_ts)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(trade_id) DO UPDATE SET
                symbol=excluded.symbol, side=excluded.side,
                setup_type=COALESCE(excluded.setup_type,trades.setup_type),
                status=CASE WHEN trades.finalized=0 THEN 'OPEN' ELSE trades.status END,
                entry_ts=COALESCE(trades.entry_ts,excluded.entry_ts),
                entry_qty=COALESCE(NULLIF(trades.actual_filled_qty,0),excluded.entry_qty,trades.entry_qty),
                entry_price=COALESCE(excluded.entry_price,trades.entry_price),
                sl_price=COALESCE(excluded.sl_price,trades.sl_price),
                remaining_qty=COALESCE(trades.remaining_qty,excluded.remaining_qty),
                actual_filled_qty=COALESCE(NULLIF(trades.actual_filled_qty,0),excluded.actual_filled_qty),
                updated_ts=excluded.updated_ts""",
                (str(trade_id), str(symbol), str(side), setup_type, "OPEN", time.time(),
                 float(qty), float(entry_price), sl_price, float(qty), float(qty), time.time()))
            self.event(trade_id, "trade_started", "TRADE_STARTED", {"qty": qty, "entry_price": entry_price}, db=db)
            db.execute("COMMIT")

    def update(self, trade_id: str, **fields: Any) -> None:
        allowed = {"status", "recovery_status", "native_sl_order_id", "native_sl_verified", "native_sl_status", "tp1_hit", "tp2_hit", "remaining_qty", "close_ts", "close_price", "close_reason", "close_confidence", "realized_pnl", "actual_filled_qty", "tp1_filled_qty", "tp1_execution_price", "tp2_filled_qty", "trailing_state", "highest_favorable", "lowest_favorable", "mfe", "profit_mode", "exchange_state", "pending_orders", "last_reconciled_ts", "lifecycle_state", "position_mode", "position_side", "entry_price", "entry_qty", "tp1_price", "tp2_price"}
        values = [(k, json.dumps(v, sort_keys=True, default=str) if k in {"trailing_state", "pending_orders"} and v is not None else v) for k, v in fields.items() if k in allowed]
        if not values:
            return
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE trades SET " + ", ".join(f"{k}=?" for k, _ in values) + ", updated_ts=? WHERE trade_id=?", [v for _, v in values] + [time.time(), trade_id])
            db.execute("COMMIT")

    def save_snapshot(self, trade_id: str, state: Dict[str, Any], event_key: Optional[str] = None,
                      event_type: str = "STATE_SNAPSHOT") -> bool:
        """Atomically persist complete management state and an idempotent checkpoint."""
        if not trade_id:
            return False
        snapshot = json.dumps(state, sort_keys=True, default=str, separators=(",", ":"))
        def val(key, default=None):
            return state.get(key, default)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""UPDATE trades SET state_json=?, status=?, remaining_qty=?, actual_filled_qty=?,
                entry_qty=COALESCE(?,entry_qty), entry_price=COALESCE(?,entry_price),
                sl_price=COALESCE(?,sl_price), tp1_price=COALESCE(?,tp1_price),
                tp2_price=COALESCE(?,tp2_price), position_mode=COALESCE(?,position_mode),
                position_side=COALESCE(?,position_side), native_sl_order_id=COALESCE(?,native_sl_order_id),
                native_sl_verified=?, native_sl_status=?, tp1_hit=?, tp1_filled_qty=?,
                tp1_execution_price=?, tp2_hit=?, tp2_filled_qty=?, trailing_state=?,
                highest_favorable=?, lowest_favorable=?, mfe=?, profit_mode=?, exchange_state=?,
                pending_orders=?, last_reconciled_ts=?, lifecycle_state=?, updated_ts=? WHERE trade_id=?""",
                (snapshot, str(val("trade_lifecycle_state", val("status", "OPEN"))),
                 val("remaining_qty", val("qty")), val("actual_filled_qty", val("initial_qty", val("qty"))),
                 val("initial_qty", val("qty")), val("entry"),
                 val("native_sl_original_price", val("synthetic_sl", val("sl"))),
                 val("tp1_price", val("synthetic_tp1")), val("tp2_price"),
                 val("position_mode"), val("position_side"), val("native_sl_order_id"),
                 int(bool(val("native_sl_verified", False))), val("native_sl_status"),
                 int(bool(val("tp1_hit", False))), val("tp1_filled_qty", 0), val("tp1_execution_price"),
                 int(bool(val("tp2_hit", False))), val("tp2_filled_qty", 0),
                 json.dumps({k: val(k) for k in ("trail_activated", "trail_on", "trail_stop", "profit_lock_activated", "be_done", "synthetic_sl")}, sort_keys=True),
                 val("max_price", val("peak_price")), val("min_price"), val("peak_roe"), val("profit_mode"),
                 val("exchange_state", "UNKNOWN"), json.dumps(val("pending_orders", []), default=str),
                 val("last_reconciled_ts"), val("trade_lifecycle_state"), time.time(), trade_id))
            inserted = self.event(trade_id, event_key or f"snapshot:{int(time.time()*1000)}", event_type,
                                  {"status": val("status", "OPEN"), "native_sl_verified": bool(val("native_sl_verified", False))}, db=db)
            db.execute("COMMIT")
            return inserted

    def event(self, trade_id: str, event_key: str, event_type: str, payload: Optional[Dict[str, Any]] = None, db=None) -> bool:
        owns = db is None
        if owns:
            ctx = self._db(); db = ctx.__enter__()
        try:
            cur = db.execute("INSERT OR IGNORE INTO trade_events(trade_id,event_key,event_type,payload,created_ts) VALUES(?,?,?,?,?)",
                             (trade_id, event_key, event_type, json.dumps(payload or {}, sort_keys=True, default=str), time.time()))
            return cur.rowcount == 1
        finally:
            if owns:
                ctx.__exit__(None, None, None)

    def finalize_once(self, trade_id: str, close_reason: str, close_price: Optional[float], realized_pnl: Optional[float], confidence: str = "EXCHANGE_CONFIRMED") -> bool:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT finalized FROM trades WHERE trade_id=?", (trade_id,)).fetchone()
            if not row or row[0]:
                db.execute("ROLLBACK")
                return False
            db.execute("""UPDATE trades SET status='CLOSED', close_ts=?, close_price=?, close_reason=?, close_confidence=?, realized_pnl=?, finalized=1, remaining_qty=0, updated_ts=? WHERE trade_id=?""",
                       (time.time(), close_price, close_reason, confidence, realized_pnl, time.time(), trade_id))
            self.event(trade_id, "finalized", "TRADE_FINALIZED", {"reason": close_reason, "confidence": confidence}, db=db)
            db.execute("COMMIT")
            return True

    def is_event_consumed(self, event_id: str) -> bool:
        if not event_id:
            return False
        with self._db() as db:
            row = db.execute("SELECT 1 FROM consumed_events WHERE event_id=?", (str(event_id),)).fetchone()
            return row is not None

    def consume_event(self, event_id: str, trade_id: str) -> bool:
        """Persist one-time event consumption across process restarts."""
        if not event_id or not trade_id:
            return False
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            cur = db.execute("INSERT OR IGNORE INTO consumed_events(event_id,trade_id,created_ts) VALUES(?,?,?)",
                             (str(event_id), str(trade_id), time.time()))
            if cur.rowcount == 1:
                self.event(str(trade_id), f"entry-event:{event_id}", "ENTRY_EVENT_CONSUMED", {"event_id": str(event_id)}, db=db)
            db.execute("COMMIT")
            return cur.rowcount == 1

    def get(self, trade_id: str) -> Optional[Dict[str, Any]]:
        with self._db() as db:
            row = db.execute("SELECT * FROM trades WHERE trade_id=?", (trade_id,)).fetchone()
            if not row:
                return None
            cols = [x[1] for x in db.execute("PRAGMA table_info(trades)").fetchall()]
            result = dict(zip(cols, row))
            if result.get("state_json"):
                try: result["state"] = json.loads(result["state_json"])
                except (TypeError, ValueError): result["state"] = {}
            else:
                result["state"] = {}
            for raw_key, parsed_key in (("trailing_state", "trailing_state_data"), ("pending_orders", "pending_orders_data")):
                try: result[parsed_key] = json.loads(result.get(raw_key) or ("{}" if raw_key == "trailing_state" else "[]"))
                except (TypeError, ValueError): result[parsed_key] = {} if raw_key == "trailing_state" else []
            return result

    def open_trades(self) -> Iterable[Dict[str, Any]]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM trades WHERE finalized=0 ORDER BY entry_ts").fetchall()
            cols = [x[1] for x in db.execute("PRAGMA table_info(trades)").fetchall()]
            results = [dict(zip(cols, row)) for row in rows]
            for result in results:
                try: result["state"] = json.loads(result.get("state_json") or "{}")
                except (TypeError, ValueError): result["state"] = {}
                try: result["trailing_state_data"] = json.loads(result.get("trailing_state") or "{}")
                except (TypeError, ValueError): result["trailing_state_data"] = {}
                try: result["pending_orders_data"] = json.loads(result.get("pending_orders") or "[]")
                except (TypeError, ValueError): result["pending_orders_data"] = []
            return results
