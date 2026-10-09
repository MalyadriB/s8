import json
import logging
import os
import base64
import queue
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any


_ready_data_dirs: set[str] = set()


def database_path() -> Path:
    """The database file, its directory created the first time it is asked for. Every storage call asks (through
    initialize_database() and get_connection(), twice per call - every tick write and every Today poll's ~18 reads), so
    the directory is made once per process and directory, not with two filesystem calls each time."""
    data_dir = os.getenv("DATA_DIR", "data")
    if data_dir not in _ready_data_dirs:
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        _ready_data_dirs.add(data_dir)
    return Path(data_dir) / os.getenv("MARKET_DATABASE", "market.db")


_wal_databases: set[Path] = set()


def get_connection() -> sqlite3.Connection:
    """Opens a connection tuned for concurrent access.

    FastAPI runs each sync route in its own thread, and several routes (the ATM ± N bulk
    option download in particular, plus live polling) can legitimately hit the database at
    the same time. SQLite's default rollback-journal mode takes an exclusive lock for the
    whole file on every write, and the default 5s busy timeout isn't always enough headroom
    under that load, which surfaced as "database is locked" 500s across unrelated endpoints.
    WAL mode lets readers proceed without blocking on writers (and vice versa), and a longer
    timeout gives writers more room to queue behind each other instead of failing outright.
    """
    path = database_path()
    connection = sqlite3.connect(path, timeout=30.0)
    # WAL is a property of the database file and persists, so it is set once per database per process; the other two
    # are per-connection settings and are set on each one
    if path not in _wal_databases:
        connection.execute("PRAGMA journal_mode=WAL")
        _wal_databases.add(path)
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


_initialized_databases: set[Path] = set()
_initialization_lock = threading.Lock()


def initialize_database() -> None:
    """Creates the schema and runs the one-time dedupe. Idempotent, and cheap after the first
    call per process: nearly every storage function calls this defensively, including the
    per-tick write path, and the dedupe below scans the whole observations table - about half a
    second on a multi-day database. Re-running it on every websocket message capped the stream
    handler at roughly two messages a second (and slower as the table grew through the day),
    which is what thinned the recorded ticks out and got the connection dropped as a slow
    consumer."""
    path = database_path()
    if path in _initialized_databases:
        return
    with _initialization_lock:
        if path in _initialized_databases:
            return
        _create_schema()
        _initialized_databases.add(path)


_STRADDLE_DAILY_DDL = """CREATE TABLE {name} (
    trading_date TEXT NOT NULL,
    expiry TEXT,
    dte INTEGER,
    is_expiry_day INTEGER,
    atm_strike REAL,
    ce_instrument_key TEXT,
    pe_instrument_key TEXT,
    spot_0915 REAL,
    spot_0930 REAL,
    spot_close REAL,
    open_high REAL,
    open_low REAL,
    rest_high REAL,
    rest_low REAL,
    ce_0930 REAL,
    pe_0930 REAL,
    straddle_0930 REAL,
    ce_close REAL,
    pe_close REAL,
    straddle_close REAL,
    realized_move_abs REAL,
    straddle_pnl REAL,
    ce_oi_0930 INTEGER,
    pe_oi_0930 INTEGER,
    ce_vol_0930 INTEGER,
    pe_vol_0930 INTEGER,
    built_at TEXT,
    build_version TEXT,
    intrinsic_close REAL,
    strike_offset REAL,
    underlying TEXT NOT NULL,
    PRIMARY KEY (underlying, trading_date)
)"""

_STRADDLE_SKIPS_DDL = """CREATE TABLE {name} (
    underlying TEXT NOT NULL,
    trading_date TEXT NOT NULL,
    reason TEXT NOT NULL,
    detail TEXT,
    logged_at TEXT NOT NULL,
    build_version TEXT,
    PRIMARY KEY (underlying, trading_date)
)"""


def _ensure_straddle_table(connection: sqlite3.Connection, table: str, ddl: str) -> None:
    """Creates a straddle table, or migrates one from before the study covered more than NIFTY.

    Those earlier tables keyed rows by trading_date alone (and the first version lacked the two
    later columns). SQLite cannot change a primary key in place, so the table is rebuilt: renamed
    aside, recreated, copied across with every row tagged 'NIFTY' (the only underlying that
    existed), and dropped - all in one transaction, so a failure leaves the old table untouched.
    """
    columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
    if not columns:
        connection.execute(ddl.format(name=table))
        return
    if "underlying" in columns:
        return
    legacy = f"{table}_legacy"
    connection.execute("BEGIN")
    try:
        connection.execute(f"ALTER TABLE {table} RENAME TO {legacy}")
        connection.execute(ddl.format(name=table))
        common = [row[1] for row in connection.execute(f"PRAGMA table_info({table})") if row[1] in columns]
        connection.execute(
            f"INSERT INTO {table} ({', '.join(common)}, underlying) SELECT {', '.join(common)}, 'NIFTY' FROM {legacy}"
        )
        connection.execute(f"DROP TABLE {legacy}")
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


def _create_schema() -> None:
    with get_connection() as connection:
        connection.executescript(
            """
            -- stream_messages (the raw message log) is gone: see append_stream_message().
            CREATE TABLE IF NOT EXISTS market_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                received_at TEXT NOT NULL,
                trading_date TEXT NOT NULL,
                instrument_key TEXT NOT NULL,
                ltp REAL,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume REAL,
                oi REAL,
                payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_observations_instrument_time
                ON market_observations(instrument_key, received_at);
            CREATE INDEX IF NOT EXISTS idx_observations_trading_date
                ON market_observations(trading_date, id, received_at, instrument_key, volume, oi);
            CREATE TABLE IF NOT EXISTS instrument_contracts (
                instrument_key TEXT PRIMARY KEY,
                expiry TEXT,
                instrument_type TEXT,
                strike_price REAL,
                trading_symbol TEXT,
                underlying_key TEXT,
                payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_contracts_lookup
                ON instrument_contracts(underlying_key, expiry, strike_price, instrument_type);
            CREATE TABLE IF NOT EXISTS stream_gaps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                occurred_at TEXT NOT NULL,
                instrument_key TEXT NOT NULL,
                gap_seconds REAL NOT NULL,
                details TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_stream_gaps_time
                ON stream_gaps(occurred_at);
            CREATE TABLE IF NOT EXISTS gamma_chain_daily (
                trading_date TEXT NOT NULL,
                expiry TEXT,
                strike REAL NOT NULL,
                moneyness REAL,
                ce_oi_1400 INTEGER,
                pe_oi_1400 INTEGER,
                ce_oi_0915 INTEGER,
                pe_oi_0915 INTEGER,
                ce_close_1400 REAL,
                pe_close_1400 REAL,
                ce_vol_1330_1400 INTEGER,
                pe_vol_1330_1400 INTEGER,
                built_at TEXT,
                build_version TEXT,
                PRIMARY KEY (trading_date, strike)
            );
            CREATE TABLE IF NOT EXISTS gamma_daily (
                trading_date TEXT PRIMARY KEY,
                spot_1400 REAL,
                spot_close REAL,
                total_oi_chain INTEGER,
                near_money_oi INTEGER,
                near_money_share REAL,
                max_oi_strike REAL,
                dist_to_max_oi_strike REAL,
                R_late REAL,
                strike_radius INTEGER,
                built_at TEXT,
                build_version TEXT
            );
            CREATE TABLE IF NOT EXISTS strategy_backtest_daily (
                strategy TEXT NOT NULL,
                trading_date TEXT NOT NULL,
                expiry TEXT,
                atm_strike REAL,
                wing_width REAL,
                lot_size INTEGER,
                lots INTEGER,
                legs TEXT,
                gross_pts REAL,
                charges_pts REAL,
                net_pts REAL,
                gross_rs REAL,
                charges_rs REAL,
                net_rs REAL,
                cost_model TEXT,
                build_version TEXT,
                built_at TEXT,
                PRIMARY KEY (strategy, trading_date)
            );
            CREATE TABLE IF NOT EXISTS strategy_nse_daily (
                strategy TEXT NOT NULL,
                trading_date TEXT NOT NULL,
                expiry TEXT,
                atm_strike REAL,
                wing_width REAL,
                lot_size INTEGER,
                lots INTEGER,
                legs TEXT,
                gross_pts REAL,
                charges_pts REAL,
                net_pts REAL,
                gross_rs REAL,
                charges_rs REAL,
                net_rs REAL,
                cost_model TEXT,
                build_version TEXT,
                built_at TEXT,
                PRIMARY KEY (strategy, trading_date)
            );
            CREATE TABLE IF NOT EXISTS strategy_backtest_skips (
                strategy TEXT NOT NULL,
                trading_date TEXT NOT NULL,
                reason TEXT NOT NULL,
                detail TEXT,
                logged_at TEXT NOT NULL,
                build_version TEXT,
                PRIMARY KEY (strategy, trading_date)
            );
            CREATE TABLE IF NOT EXISTS paper_trades (
                strategy TEXT NOT NULL,
                trading_date TEXT NOT NULL,
                expiry TEXT,
                lots INTEGER,
                lot_size INTEGER,
                status TEXT NOT NULL,
                atm_strike REAL,
                spot_at_entry REAL,
                premium_ref REAL,
                wing_width REAL,
                legs TEXT,
                gross_pts REAL,
                charges_pts REAL,
                net_pts REAL,
                net_rs REAL,
                exit_delay_s REAL,
                notes TEXT,
                build_version TEXT,
                created_at TEXT,
                updated_at TEXT,
                bt_gross_pts REAL,
                bt_charges_pts REAL,
                bt_net_pts REAL,
                bt_net_rs REAL,
                bt_legs TEXT,
                bt_note TEXT,
                reconciled_at TEXT,
                invalid_reason TEXT,
                PRIMARY KEY (strategy, trading_date)
            );
            CREATE TABLE IF NOT EXISTS paper_marks (
                strategy TEXT NOT NULL,
                trading_date TEXT NOT NULL,
                minute TEXT NOT NULL,
                mtm_rs REAL,
                PRIMARY KEY (strategy, trading_date, minute)
            );
            CREATE TABLE IF NOT EXISTS paper_health (
                trading_date TEXT PRIMARY KEY,
                checked_at TEXT,
                ok INTEGER,
                detail TEXT
            );
            CREATE TABLE IF NOT EXISTS paper_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                trading_date TEXT,
                level TEXT NOT NULL,
                message TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS gamma_build_skips (
                trading_date TEXT PRIMARY KEY,
                reason TEXT NOT NULL,
                detail TEXT,
                logged_at TEXT NOT NULL,
                build_version TEXT
            );
            """
        )
        _ensure_straddle_table(connection, "straddle_daily", _STRADDLE_DAILY_DDL)
        _ensure_straddle_table(connection, "straddle_build_skips", _STRADDLE_SKIPS_DDL)
        # idx_observations_trading_date grew extra columns (received_at, instrument_key, volume, oi) so a query
        # filtered by trading_date - analytics, reading a whole day's ticks - can be answered entirely from the
        # index (a "covering" index) instead of also visiting the matching row in the main table, which meant
        # reading that row's full payload blob too on every match: by far the slowest part of loading a busy
        # day's Analysis screen. CREATE INDEX IF NOT EXISTS never widens an index that already exists under this
        # name, so an older, narrower version left over from before this change is dropped and rebuilt here.
        existing = connection.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_observations_trading_date'").fetchone()
        if existing and existing[0] and "received_at" not in existing[0]:
            connection.execute("DROP INDEX idx_observations_trading_date")
            connection.execute("CREATE INDEX idx_observations_trading_date ON market_observations(trading_date, id, received_at, instrument_key, volume, oi)")
        # A row is uniquely identified by (instrument_key, received_at). Historical
        # re-downloads used to append duplicate rows for the same instant on every
        # re-click of Load Data, which lightweight-charts rejects outright (it requires
        # strictly ascending, non-repeating timestamps) and crashed the whole chart.
        # Dedupe anything already on disk from before this constraint existed, then
        # enforce it going forward so every insert path can safely upsert instead of append.
        #
        # The dedupe DELETE is a full-table GROUP BY scan - once idx_observations_unique
        # exists, uniqueness is already enforced going forward, so there is nothing left for
        # it to ever find. initialize_database()'s "already initialized" guard is per-process
        # (an in-memory set), so without this check the scan re-ran on every single
        # `--reload` restart - on a market_observations table that has grown into tens of GB
        # from the live feed, that alone was multi-minute (or worse, under write contention)
        # dead time on every code edit, well before any request-handling code even ran.
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_observations_unique'"
        ).fetchone():
            connection.execute(
                """DELETE FROM market_observations
                WHERE id NOT IN (SELECT MIN(id) FROM market_observations GROUP BY instrument_key, received_at)"""
            )
            connection.execute(
                """CREATE UNIQUE INDEX idx_observations_unique
                ON market_observations(instrument_key, received_at)"""
            )
        # paper_trades gained invalid_reason after some databases already had the table without it (a paper fill
        # that could not represent its intended exit moment - see paper.py's PaperEngine._try_exit()); CREATE
        # TABLE IF NOT EXISTS above is a no-op on those, so add the column here if it is still missing.
        columns = [row[1] for row in connection.execute("PRAGMA table_info(paper_trades)")]
        if "invalid_reason" not in columns:
            connection.execute("ALTER TABLE paper_trades ADD COLUMN invalid_reason TEXT")


class _JsonlAppender:
    """Appends lines to the day's JSONL files on a background thread, keeping each file open.

    The stream's own thread used to open, write and CLOSE a file for every message (twice: the stream file and the gaps
    file), and profiling the live worker showed that thread sitting in the operating system's file-close call for most of
    its time - so it processed only a handful of messages a minute while the exchange sent far more, and the connection
    backed up and was eventually dropped. Now the handler only queues the text; nothing on the stream's thread waits on
    the file system for these files."""

    def __init__(self) -> None:
        self._queue: queue.Queue[tuple[Path | None, Any]] = queue.Queue()
        self._handles: dict[Path, Any] = {}
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()

    def write(self, path: Path, text: str) -> None:
        with self._start_lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="jsonl-appender", daemon=True)
                self._thread.start()
        self._queue.put((path, text))

    def flush(self, timeout: float = 15.0) -> None:
        """Wait until everything queued so far is on disk (tests, shutdown)."""
        if self._thread is None or not self._thread.is_alive():
            return
        done = threading.Event()
        self._queue.put((None, done))
        done.wait(timeout)

    def close_all(self) -> None:
        self.flush()
        for path in list(self._handles):
            self._drop(path)

    def _drop(self, path: Path) -> None:
        handle = self._handles.pop(path, None)
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass

    def _run(self) -> None:
        while True:
            batch = [self._queue.get()]
            while True:
                try:
                    batch.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            touched: set[Path] = set()
            for path, payload in batch:
                if path is None:
                    self._flush_handles(touched)
                    touched.clear()
                    payload.set()
                    continue
                try:
                    handle = self._handles.get(path)
                    if handle is None:
                        handle = self._handles[path] = path.open("a", encoding="utf-8")
                        kind = path.name.split("-")[0]
                        for old in [known for known in self._handles if known != path and known.name.split("-")[0] == kind]:
                            self._drop(old)  # an earlier day's file of the same kind
                    handle.write(payload)
                    touched.add(path)
                except OSError:
                    logging.getLogger("uvicorn.error").exception("Could not append to %s", path)
                    self._drop(path)
            self._flush_handles(touched)

    def _flush_handles(self, paths: set[Path]) -> None:
        for path in paths:
            handle = self._handles.get(path)
            if handle is not None:
                try:
                    handle.flush()
                except OSError:
                    self._drop(path)


_jsonl = _JsonlAppender()


def flush_jsonl() -> None:
    _jsonl.flush()


def close_jsonl_files() -> None:
    _jsonl.close_all()


def append_stream_message(message: Any, received_at: datetime) -> list[dict[str, Any]]:
    """Store one stream event's per-instrument prices in market_observations - what the paper engine, the Today screen
    and the day charts read - and return them as plain dicts.

    The raw message itself is not kept. It used to be written twice more - a daily stream-YYYY-MM-DD.jsonl file and the
    stream_messages table, the whole message once per instrument in it - for the research views. Nothing has read
    either since those were removed, and together they reached ~100 GB and filled the D: drive on 2026-10-07."""
    normalized = normalize_stream_message(message)
    initialize_database()
    rows = [
        _observation_row(received_at, instrument_key, feed)
        for instrument_key, feed in _feed_items(normalized)
    ]
    with get_connection() as connection:
        connection.executemany(
            """INSERT OR REPLACE INTO market_observations
            (received_at, trading_date, instrument_key, ltp, open, high, low, close, volume, oi, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
    observations = [
        {
            "received_at": row[0],
            "trading_date": row[1],
            "instrument_key": row[2],
            "ltp": row[3],
            "open": row[4],
            "high": row[5],
            "low": row[6],
            "close": row[7],
            "volume": row[8],
            "oi": row[9],
        }
        for row in rows
        if row[2] != "__unknown__"
    ]
    return observations


def _observation_row(received_at: datetime, instrument_key: str, feed: Any) -> tuple[Any, ...]:
    fields = observation_fields(feed)
    return (
        received_at.isoformat(), received_at.date().isoformat(), str(instrument_key),
        fields["ltp"], fields["open"], fields["high"], fields["low"],
        fields["close"], fields["volume"], fields["oi"], json.dumps(feed),
    )


def _feed_items(normalized: Any) -> list[tuple[str, Any]]:
    if isinstance(normalized, dict) and isinstance(normalized.get("feeds"), dict):
        return [(str(key), value) for key, value in normalized["feeds"].items()]
    return [("__unknown__", normalized)]


def extract_number(value: Any, names: tuple[str, ...]) -> float | None:
    if isinstance(value, dict):
        for name in names:
            candidate = value.get(name)
            if isinstance(candidate, (int, float)):
                return float(candidate)
            nested = extract_number(candidate, names)
            if nested is not None:
                return nested
        for nested_value in value.values():
            result = extract_number(nested_value, names)
            if result is not None:
                return result
    elif isinstance(value, list):
        for item in value:
            result = extract_number(item, names)
            if result is not None:
                return result
    return None


def observation_fields(feed: Any) -> dict[str, float | None]:
    """Extract documented LTPC/full-feed values while retaining raw payloads.

    Upstox's actual V3 full-feed shape (confirmed against a live connection) is:
        fullFeed.marketFF.{ltpc, marketOHLC.ohlc[], vtt, oi}   -- futures/options/equities
        fullFeed.indexFF.{ltpc, marketOHLC.ohlc[]}             -- indices (no vtt/oi)
    `ltpc` and `marketOHLC` live one level under marketFF/indexFF, not at the top of `feed`.
    `marketOHLC.ohlc` is a LIST of per-interval candles (an "I1" ~1-minute bar and a "1d"
    running-day bar), not a flat {open, high, low} dict, and ltpc's "cp" is the *previous*
    day's close, not a live one - all three were previously read from the wrong place/shape,
    which silently kept open/high/low/close null (and close pinned to yesterday) for every
    instrument regardless of stream mode.
    """
    full_feed = feed.get("fullFeed", {}) if isinstance(feed, dict) else {}
    market_feed = (full_feed.get("marketFF") or full_feed.get("indexFF")) if isinstance(full_feed, dict) else None
    if not isinstance(market_feed, dict):
        market_feed = {}
    ltpc = market_feed.get("ltpc", {})
    if not isinstance(ltpc, dict):
        ltpc = {}

    ohlc_entries = market_feed.get("marketOHLC", {})
    candles = ohlc_entries.get("ohlc", []) if isinstance(ohlc_entries, dict) else []
    minute_candle = next((c for c in candles if isinstance(c, dict) and c.get("interval") != "1d"), None)
    day_candle = next((c for c in candles if isinstance(c, dict) and c.get("interval") == "1d"), None)
    live_candle = minute_candle or day_candle or {}

    return {
        "ltp": _number(ltpc.get("ltp")) or extract_number(feed, ("ltp", "last_price")),
        "open": _number(live_candle.get("open")),
        "high": _number(live_candle.get("high")),
        "low": _number(live_candle.get("low")),
        "close": _number(live_candle.get("close")) or _number(ltpc.get("ltp")),
        "volume": _number(market_feed.get("vtt")) or _number(market_feed.get("volume")) or extract_number(feed, ("volume", "volume_traded")),
        "oi": _number(market_feed.get("oi")) or extract_number(feed, ("oi", "open_interest")),
    }


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def append_gap_events(gaps: list[dict[str, Any]], occurred_at: datetime) -> Path | None:
    """Record every gap found in one stream message with ONE file write and ONE transaction.

    This runs on the stream's own thread for every message, and one message can find dozens of quiet instruments. Opening
    the file and a fresh SQLite connection per gap made the handler slower than the feed arrives (it spent ~80% of its time
    here), so the connection backed up, delivered a thin, delayed feed and was eventually dropped."""
    if not gaps:
        return None
    stream_dir = Path(os.getenv("STREAM_DATA_DIR", os.getenv("DATA_DIR", "data")))
    stream_dir.mkdir(parents=True, exist_ok=True)
    path = stream_dir / f"gaps-{occurred_at:%Y-%m-%d}.jsonl"
    _jsonl.write(path, "".join(json.dumps(gap, separators=(",", ":")) + "\n" for gap in gaps))
    initialize_database()
    with get_connection() as connection:
        connection.executemany(
            "INSERT INTO stream_gaps (occurred_at, instrument_key, gap_seconds, details) VALUES (?, ?, ?, ?)",
            [(occurred_at.isoformat(), str(gap.get("instrument_key", "__unknown__")), float(gap.get("gap_seconds", 0)), json.dumps(gap)) for gap in gaps],
        )
    return path


def append_gap_event(gap: dict[str, Any], occurred_at: datetime) -> Path:
    path = append_gap_events([gap], occurred_at)
    assert path is not None
    return path


def _row_kind_clause(kind: str | None) -> str:
    """SQL fragment separating downloaded candles from live-stream ticks in market_observations.

    Both live in one table, but their `received_at` differ in a reliable way: candle timestamps
    come from Upstox at whole-second precision ("...T09:15:00+05:30"), while live ticks are
    stamped with datetime.now() and carry microseconds ("...T09:15:23.456789+05:30"). Checking
    for the fractional-second dot uses only the indexed column - the alternative, peeking at
    the payload's first character, would force SQLite to read every tick's multi-KB feed blob.
    """
    if kind == "candles":
        return " AND instr(received_at, '.') = 0"
    if kind == "ticks":
        return " AND instr(received_at, '.') > 0"
    return ""


def save_instrument_contracts(contracts: list[dict[str, Any]]) -> None:
    initialize_database()
    with get_connection() as connection:
        connection.executemany(
            """INSERT OR REPLACE INTO instrument_contracts
            (instrument_key, expiry, instrument_type, strike_price, trading_symbol, underlying_key, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    item["instrument_key"], item.get("expiry"), item.get("instrument_type"),
                    item.get("strike_price"), item.get("trading_symbol"),
                    item.get("underlying_key"), json.dumps(item),
                )
                for item in contracts
                if item.get("instrument_key")
            ],
        )


def query_candle_bars(instrument_key: str, trading_date: str) -> list[dict[str, Any]]:
    """One instrument's downloaded candles for one day, oldest first. Live-stream ticks are
    excluded (see _row_kind_clause) and so are tick-like rows without an open.

    The received_at bounds (every timestamp of that date sorts between "dayT" and "dayU") let SQLite seek the
    (instrument_key, received_at) index straight to the day; "+trading_date" keeps the date filter without letting it
    pick the trading_date index, which would walk every row of a live-feed day."""
    initialize_database()
    with get_connection() as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT received_at, open, high, low, close, volume, oi FROM market_observations"
            " WHERE instrument_key = ? AND +trading_date = ? AND received_at >= ? AND received_at < ? AND open IS NOT NULL"
            + _row_kind_clause("candles")
            + " ORDER BY received_at",
            (instrument_key, trading_date, f"{trading_date}T", f"{trading_date}U"),
        ).fetchall()
    return [dict(row) for row in rows]


def known_option_expiries(underlying_key: str) -> list[str]:
    initialize_database()
    with get_connection() as connection:
        rows = connection.execute(
            """SELECT DISTINCT expiry FROM instrument_contracts
            WHERE underlying_key = ? AND instrument_type IN ('CE', 'PE') AND expiry IS NOT NULL
            ORDER BY expiry""",
            (underlying_key,),
        ).fetchall()
    return [row[0] for row in rows]


def find_option_contract(underlying_key: str, expiry: str, strike: float, option_type: str, *, expired: bool) -> str | None:
    """The instrument key of one option contract, or None if instrument_contracts has no such row.

    An expiry can be stored under two key shapes - a live-search contract ("NSE_FO|56984") and,
    once it has lapsed, its expired-contract twin ("NSE_FO|56984|15-09-2026"). Only the twin is
    accepted by Upstox's expired-candle endpoint (and only the plain one by the live endpoint),
    so the caller says which it needs.
    """
    initialize_database()
    with get_connection() as connection:
        rows = connection.execute(
            """SELECT instrument_key FROM instrument_contracts
            WHERE underlying_key = ? AND expiry = ? AND strike_price = ? AND instrument_type = ?""",
            (underlying_key, expiry, strike, option_type),
        ).fetchall()
    for (key,) in rows:
        if (key.count("|") == 2) == expired:
            return key
    return None


def find_option_contracts_bulk(underlying_key: str, targets: list[tuple[str, float, str]]) -> dict[tuple[str, float, str], list[str]]:
    """{(expiry, strike, instrument_type): [instrument_key, ...]} for every (expiry, strike, instrument_type) in
    `targets` that has at least one row in instrument_contracts (live-shaped, expired-shaped, or both - the caller
    tells them apart by key.count("|"), same as find_option_contract()) - ONE query for the whole batch instead of
    one (or several - a caller checking both shapes) per target.

    strategy_lab.py's roll/wing resolution used to call find_option_contract() once per historical entrant - fine
    in isolation, but with a live feed writing to the same database continuously, a few hundred individual
    connections in a tight loop can each stall on lock contention, turning a sub-second lookup into minutes. This
    exists so that hot path can resolve everything it is going to need in one round trip instead."""
    if not targets:
        return {}
    initialize_database()
    expiries = sorted({expiry for expiry, _, _ in targets})
    strikes = sorted({strike for _, strike, _ in targets})
    types = sorted({option_type for _, _, option_type in targets})
    wanted = set(targets)
    e_marks, s_marks, t_marks = ",".join("?" * len(expiries)), ",".join("?" * len(strikes)), ",".join("?" * len(types))
    with get_connection() as connection:
        rows = connection.execute(
            f"""SELECT expiry, strike_price, instrument_type, instrument_key FROM instrument_contracts
            WHERE underlying_key = ? AND expiry IN ({e_marks}) AND strike_price IN ({s_marks}) AND instrument_type IN ({t_marks})""",
            (underlying_key, *expiries, *strikes, *types),
        ).fetchall()
    out: dict[tuple[str, float, str], list[str]] = {}
    for expiry, strike, option_type, key in rows:
        target = (expiry, strike, option_type)
        if target in wanted:
            out.setdefault(target, []).append(key)
    return out


def contract_type_strike(instrument_key: str) -> tuple[str, float] | None:
    """(instrument_type, strike_price) of one already-listed contract, or None if it is not in
    instrument_contracts - the reverse lookup of find_option_contract(), for when a caller has a
    key (e.g. from a stored leg) and needs what it actually is to look up its OTHER key shape."""
    initialize_database()
    with get_connection() as connection:
        row = connection.execute(
            "SELECT instrument_type, strike_price FROM instrument_contracts WHERE instrument_key = ?", (instrument_key,)
        ).fetchone()
    return (row[0], row[1]) if row else None


STRADDLE_COLUMNS = [
    "trading_date", "expiry", "dte", "is_expiry_day", "atm_strike", "ce_instrument_key", "pe_instrument_key",
    "spot_0915", "spot_0930", "spot_close", "open_high", "open_low", "rest_high", "rest_low",
    "ce_0930", "pe_0930", "straddle_0930", "ce_close", "pe_close", "straddle_close",
    "realized_move_abs", "straddle_pnl", "ce_oi_0930", "pe_oi_0930", "ce_vol_0930", "pe_vol_0930",
    "built_at", "build_version", "intrinsic_close", "strike_offset", "underlying",
]


def upsert_straddle_row(row: dict[str, Any]) -> None:
    initialize_database()
    with get_connection() as connection:
        connection.execute(
            f"INSERT OR REPLACE INTO straddle_daily ({', '.join(STRADDLE_COLUMNS)}) VALUES ({', '.join('?' * len(STRADDLE_COLUMNS))})",
            [row[column] for column in STRADDLE_COLUMNS],
        )
        connection.execute(
            "DELETE FROM straddle_build_skips WHERE underlying = ? AND trading_date = ?", (row["underlying"], row["trading_date"])
        )


def query_straddle_rows(from_date: str | None = None, to_date: str | None = None, *, underlying: str | None = None) -> list[dict[str, Any]]:
    initialize_database()
    clauses, parameters = [], []
    if underlying:
        clauses.append("underlying = ?")
        parameters.append(underlying)
    if from_date:
        clauses.append("trading_date >= ?")
        parameters.append(from_date)
    if to_date:
        clauses.append("trading_date <= ?")
        parameters.append(to_date)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with get_connection() as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            f"SELECT {', '.join(STRADDLE_COLUMNS)} FROM straddle_daily {where} ORDER BY underlying, trading_date", parameters
        ).fetchall()
    return [dict(row) for row in rows]


BACKTEST_COLUMNS = [
    "strategy", "trading_date", "expiry", "atm_strike", "wing_width", "lot_size", "lots", "legs", "gross_pts", "charges_pts",
    "net_pts", "gross_rs", "charges_rs", "net_rs", "cost_model", "build_version", "built_at",
]
PAPER_TRADE_COLUMNS = [
    "strategy", "trading_date", "expiry", "lots", "lot_size", "status", "atm_strike", "spot_at_entry", "premium_ref", "wing_width",
    "legs", "gross_pts", "charges_pts", "net_pts", "net_rs", "exit_delay_s", "notes", "build_version", "created_at", "updated_at",
    "bt_gross_pts", "bt_charges_pts", "bt_net_pts", "bt_net_rs", "bt_legs", "bt_note", "reconciled_at", "invalid_reason",
]
_JSON_COLUMNS = ("legs", "bt_legs")


def _decode(row: sqlite3.Row) -> dict[str, Any]:
    decoded = dict(row)
    for column in _JSON_COLUMNS:
        if column in decoded and isinstance(decoded[column], str):
            decoded[column] = json.loads(decoded[column])
    return decoded


def _encode(row: dict[str, Any], columns: list[str]) -> list[Any]:
    return [json.dumps(row.get(column)) if column in _JSON_COLUMNS and row.get(column) is not None else row.get(column) for column in columns]


def _range_query(table: str, columns: list[str], strategy: str | None, from_date: str | None, to_date: str | None, order: str) -> list[dict[str, Any]]:
    initialize_database()
    clauses, parameters = [], []
    for clause, value in (("strategy = ?", strategy), ("trading_date >= ?", from_date), ("trading_date <= ?", to_date)):
        if value:
            clauses.append(clause)
            parameters.append(value)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with get_connection() as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(f"SELECT {', '.join(columns)} FROM {table} {where} ORDER BY {order}", parameters).fetchall()
    return [_decode(row) for row in rows]


def upsert_backtest_row(row: dict[str, Any]) -> None:
    initialize_database()
    with get_connection() as connection:
        connection.execute(
            f"INSERT OR REPLACE INTO strategy_backtest_daily ({', '.join(BACKTEST_COLUMNS)}) VALUES ({', '.join('?' * len(BACKTEST_COLUMNS))})",
            _encode(row, BACKTEST_COLUMNS),
        )
        connection.execute("DELETE FROM strategy_backtest_skips WHERE strategy = ? AND trading_date = ?", (row["strategy"], row["trading_date"]))


def query_backtest_rows(strategy: str | None = None, from_date: str | None = None, to_date: str | None = None) -> list[dict[str, Any]]:
    return _range_query("strategy_backtest_daily", BACKTEST_COLUMNS, strategy, from_date, to_date, "strategy, trading_date")


def upsert_nse_daily_row(row: dict[str, Any]) -> None:
    """One day of a strategy on NSE's END-OF-DAY prices (entered at the day's open, exited at its close), from Jan 2022. A separate table from
    strategy_backtest_daily (the 09:30 -> 15:29 minute backtest): the two are different measurements and are never mixed."""
    initialize_database()
    with get_connection() as connection:
        connection.execute(
            f"INSERT OR REPLACE INTO strategy_nse_daily ({', '.join(BACKTEST_COLUMNS)}) VALUES ({', '.join('?' * len(BACKTEST_COLUMNS))})",
            _encode(row, BACKTEST_COLUMNS),
        )


def query_nse_daily_rows(strategy: str | None = None, from_date: str | None = None, to_date: str | None = None) -> list[dict[str, Any]]:
    return _range_query("strategy_nse_daily", BACKTEST_COLUMNS, strategy, from_date, to_date, "strategy, trading_date")


def record_backtest_skip(strategy: str, trading_date: str, reason: str, detail: str, build_version: str) -> None:
    initialize_database()
    with get_connection() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO strategy_backtest_skips (strategy, trading_date, reason, detail, logged_at, build_version) VALUES (?, ?, ?, ?, ?, ?)",
            (strategy, trading_date, reason, detail, datetime.now().isoformat(timespec="seconds"), build_version),
        )


def query_backtest_skips(strategy: str) -> dict[str, dict[str, Any]]:
    initialize_database()
    with get_connection() as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT strategy, trading_date, reason, detail, logged_at, build_version FROM strategy_backtest_skips WHERE strategy = ?", (strategy,)
        ).fetchall()
    return {row["trading_date"]: dict(row) for row in rows}


def paper_insert_trade(row: dict[str, Any]) -> bool:
    """Inserts a paper trade unless one already exists for (strategy, trading_date). The primary key is what
    guarantees a strategy can never enter twice on a day, whatever restarts happen. True if it inserted."""
    initialize_database()
    with get_connection() as connection:
        cursor = connection.execute(
            f"INSERT OR IGNORE INTO paper_trades ({', '.join(PAPER_TRADE_COLUMNS)}) VALUES ({', '.join('?' * len(PAPER_TRADE_COLUMNS))})",
            _encode(row, PAPER_TRADE_COLUMNS),
        )
        return cursor.rowcount == 1


def paper_update_trade(strategy: str, trading_date: str, fields: dict[str, Any]) -> None:
    unknown = set(fields) - set(PAPER_TRADE_COLUMNS)
    if unknown:
        raise ValueError(f"unknown paper_trades columns: {sorted(unknown)}")
    if not fields:
        return
    initialize_database()
    fields = {**fields, "updated_at": datetime.now().isoformat(timespec="seconds")}
    columns = list(fields)
    with get_connection() as connection:
        connection.execute(
            f"UPDATE paper_trades SET {', '.join(f'{c} = ?' for c in columns)} WHERE strategy = ? AND trading_date = ?",
            [*_encode(fields, columns), strategy, trading_date],
        )


def paper_get_trade(strategy: str, trading_date: str) -> dict[str, Any] | None:
    rows = _range_query("paper_trades", PAPER_TRADE_COLUMNS, strategy, trading_date, trading_date, "strategy")
    return rows[0] if rows else None


def query_paper_trades(strategy: str | None = None, from_date: str | None = None, to_date: str | None = None) -> list[dict[str, Any]]:
    return _range_query("paper_trades", PAPER_TRADE_COLUMNS, strategy, from_date, to_date, "trading_date, strategy")


def paper_upsert_mark(strategy: str, trading_date: str, minute: str, mtm_rs: float | None) -> None:
    initialize_database()
    with get_connection() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO paper_marks (strategy, trading_date, minute, mtm_rs) VALUES (?, ?, ?, ?)", (strategy, trading_date, minute, mtm_rs)
        )


def query_paper_marks(trading_date: str, strategy: str | None = None) -> list[dict[str, Any]]:
    initialize_database()
    clauses, parameters = ["trading_date = ?"], [trading_date]
    if strategy:
        clauses.append("strategy = ?")
        parameters.append(strategy)
    with get_connection() as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            f"SELECT strategy, trading_date, minute, mtm_rs FROM paper_marks WHERE {' AND '.join(clauses)} ORDER BY strategy, minute", parameters
        ).fetchall()
    return [dict(row) for row in rows]


def paper_get_health(trading_date: str) -> dict[str, Any] | None:
    initialize_database()
    with get_connection() as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT trading_date, checked_at, ok, detail FROM paper_health WHERE trading_date = ?", (trading_date,)).fetchone()
    return dict(row) if row else None


def paper_set_health(trading_date: str, ok: bool, detail: str) -> None:
    initialize_database()
    with get_connection() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO paper_health (trading_date, checked_at, ok, detail) VALUES (?, ?, ?, ?)",
            (trading_date, datetime.now().isoformat(timespec="seconds"), int(ok), detail),
        )


def paper_add_alert(trading_date: str | None, level: str, message: str) -> None:
    initialize_database()
    with get_connection() as connection:
        connection.execute(
            "INSERT INTO paper_alerts (ts, trading_date, level, message) VALUES (?, ?, ?, ?)",
            (datetime.now().isoformat(timespec="seconds"), trading_date, level, message),
        )


def query_paper_alerts(limit: int = 20) -> list[dict[str, Any]]:
    initialize_database()
    with get_connection() as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT ts, trading_date, level, message FROM paper_alerts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def query_latest_tick(instrument_key: str, since: str) -> dict[str, Any] | None:
    """The newest live-stream tick for an instrument received at or after `since` (an ISO timestamp), as
    {"received_at", "payload"}. Ticks carry a fractional second; downloaded candles do not."""
    initialize_database()
    with get_connection() as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT received_at, payload FROM market_observations WHERE instrument_key = ? AND received_at >= ? AND instr(received_at, '.') > 0"
            " ORDER BY received_at DESC LIMIT 1",
            (instrument_key, since),
        ).fetchone()
    return {"received_at": row["received_at"], "payload": json.loads(row["payload"])} if row else None


def save_historical_candles(instrument_key: str, candles: list[list[Any]]) -> int:
    initialize_database()
    saved = 0
    with get_connection() as connection:
        for candle in candles:
            if len(candle) < 6:
                continue
            timestamp = datetime.fromisoformat(str(candle[0]))
            values = list(candle[1:]) + [None] * 6
            connection.execute(
                """INSERT OR REPLACE INTO market_observations
                (received_at, trading_date, instrument_key, ltp, open, high, low, close, volume, oi, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    timestamp.isoformat(),
                    timestamp.date().isoformat(),
                    instrument_key,
                    values[3],
                    values[0],
                    values[1],
                    values[2],
                    values[3],
                    values[4],
                    values[5],
                    json.dumps(candle),
                ),
            )
            saved += 1
    return saved


def normalize_stream_message(message: Any) -> Any:
    if isinstance(message, (str, int, float, bool)) or message is None:
        return message
    if isinstance(message, bytes):
        return {"encoding": "base64", "value": base64.b64encode(message).decode("ascii")}
    if isinstance(message, dict):
        return {str(key): normalize_stream_message(value) for key, value in message.items()}
    if isinstance(message, (list, tuple)):
        return [normalize_stream_message(value) for value in message]
    try:
        from google.protobuf.json_format import MessageToDict

        return MessageToDict(message, preserving_proto_field_name=True)
    except (ImportError, TypeError, AttributeError):
        return {"type": type(message).__name__, "value": str(message)}