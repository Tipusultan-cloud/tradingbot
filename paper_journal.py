import csv
import math
import os
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from project_paths import DATA_DIR


DEFAULT_JOURNAL_PATH = DATA_DIR / "paper_trades.sqlite3"
PAPER_TRADES_CSV = DATA_DIR / "paper_trades.csv"
# One dated copy of the trade CSV per day (save_daily_copy).
TRADE_HISTORY_DIR = DATA_DIR / "trade_history"
DUPLICATE_WINDOW_SECONDS = 60
TRADE_CSV_FIELDS = (
    "id",
    "ticker",
    "entry_time",
    "entry_price",
    "atr",
    "shares",
    "stop_price",
    "target_price",
    "setup_score",
    "status",
    "exit_time",
    "exit_price",
    "realized_pnl",
    "exit_reason",
    "strategy",
    "entry_source",
    "broker_order_id",
    "target2_price",
    "active_stop_price",
    "partial_shares",
    "partial_exit_price",
    "partial_exit_time",
    "half_order_id",
    "rest_order_id",
    "sector",
    "entry_note",
    "exit_note",
)

# Columns added after the first journals were created.
ADDED_COLUMNS = {
    "strategy": "TEXT",
    "entry_source": "TEXT",
    "broker_order_id": "TEXT",
    # Split exit: half at the 2R target, the rest at target2_price (2.5R).
    # NULL target2_price = an older trade with a single 2R target.
    "target2_price": "REAL",
    # Stop after the half is sold (entry price); NULL = stop_price.
    "active_stop_price": "REAL",
    "partial_shares": "INTEGER",
    "partial_exit_price": "REAL",
    "partial_exit_time": "TEXT",
    # Alpaca OCO sell orders (limit target + stop) for each part.
    "half_order_id": "TEXT",
    "rest_order_id": "TEXT",
    # One trade per sector at a time (trade_manager.MAX_TRADES_PER_SECTOR).
    "sector": "TEXT",
    # Trade journal notes: why the trade was taken (filled in from the
    # bot's reason at entry, editable) and what was learned at the exit.
    "entry_note": "TEXT",
    "exit_note": "TEXT",
}
BROKER_ORDER_ADDED_COLUMNS = {"target2_price": "REAL", "sector": "TEXT", "entry_note": "TEXT"}
NOTE_FIELDS = ("entry_note", "exit_note")
MAX_NOTE_LENGTH = 2000


class DuplicateSignalError(Exception):
    pass


class OpenTradeExistsError(Exception):
    pass


class PaperTradeExportError(OSError):
    pass


def _database_path(database_path=None):
    return Path(
        database_path
        or os.getenv("PAPER_JOURNAL_PATH")
        or DEFAULT_JOURNAL_PATH
    )


def _connect(database_path=None):
    path = _database_path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


@contextmanager
def _connection(database_path=None):
    connection = _connect(database_path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize_journal(database_path=None):
    with _connection(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                entry_time TEXT NOT NULL,
                entry_price REAL NOT NULL,
                atr REAL NOT NULL,
                shares INTEGER NOT NULL,
                stop_price REAL NOT NULL,
                target_price REAL NOT NULL,
                setup_score REAL,
                status TEXT NOT NULL CHECK (status IN ('OPEN', 'CLOSED')),
                exit_time TEXT,
                exit_price REAL,
                realized_pnl REAL,
                exit_reason TEXT,
                strategy TEXT,
                entry_source TEXT,
                broker_order_id TEXT,
                target2_price REAL,
                active_stop_price REAL,
                partial_shares INTEGER,
                partial_exit_price REAL,
                partial_exit_time TEXT,
                half_order_id TEXT,
                rest_order_id TEXT
            )
            """
        )
        _add_missing_columns(connection, "paper_trades", ADDED_COLUMNS)
        # Broker orders sent but not yet filled; a fill becomes a trade.
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS broker_orders (
                order_id TEXT PRIMARY KEY,
                ticker TEXT NOT NULL,
                strategy TEXT,
                entry_source TEXT,
                shares INTEGER NOT NULL,
                atr REAL NOT NULL,
                stop_price REAL NOT NULL,
                target_price REAL NOT NULL,
                setup_score REAL,
                status TEXT NOT NULL
                    CHECK (status IN ('PENDING', 'FILLED', 'CANCELED')),
                detail TEXT,
                trade_id INTEGER REFERENCES paper_trades(id),
                submitted_at TEXT NOT NULL,
                target2_price REAL
            )
            """
        )
        _add_missing_columns(connection, "broker_orders", BROKER_ORDER_ADDED_COLUMNS)
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS one_open_paper_trade_per_ticker
            ON paper_trades(ticker) WHERE status = 'OPEN'
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS last_paper_signals (
                ticker TEXT NOT NULL,
                action TEXT NOT NULL CHECK (action IN ('BUY', 'SELL')),
                received_at REAL NOT NULL,
                PRIMARY KEY (ticker, action)
            )
            """
        )


def _add_missing_columns(connection, table, wanted):
    columns = {
        row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
    }
    for column, kind in wanted.items():
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")


def _validate_positive(name, value):
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be finite and greater than zero")
    return number


def _claim_signal(connection, ticker, action, received_at):
    previous = connection.execute(
        """
        SELECT received_at FROM last_paper_signals
        WHERE ticker = ? AND action = ?
        """,
        (ticker, action),
    ).fetchone()
    if (
        previous
        and received_at - previous["received_at"] < DUPLICATE_WINDOW_SECONDS
    ):
        raise DuplicateSignalError("Duplicate signal blocked")

    connection.execute(
        """
        INSERT INTO last_paper_signals(ticker, action, received_at)
        VALUES (?, ?, ?)
        ON CONFLICT(ticker, action)
        DO UPDATE SET received_at = excluded.received_at
        """,
        (ticker, action, received_at),
    )


def export_paper_trades_csv(database_path=None, export_path=None):
    database_path = _database_path(database_path)
    export_path = Path(export_path or (
        PAPER_TRADES_CSV
        if database_path == DEFAULT_JOURNAL_PATH
        else database_path.with_suffix(".csv")
    ))
    temporary_path = None
    try:
        export_path.parent.mkdir(parents=True, exist_ok=True)
        with _connection(database_path) as connection:
            # Every CSV column (sector and the notes were left out before
            # 2026-11-05, so they were always blank).
            trades = connection.execute(
                f"SELECT {', '.join(TRADE_CSV_FIELDS)} FROM paper_trades ORDER BY id"
            ).fetchall()
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8-sig",
            newline="",
            dir=export_path.parent,
            prefix=f".{export_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as csv_file:
            temporary_path = Path(csv_file.name)
            writer = csv.DictWriter(
                csv_file,
                fieldnames=TRADE_CSV_FIELDS,
                extrasaction="raise",
            )
            writer.writeheader()
            writer.writerows(dict(trade) for trade in trades)
            csv_file.flush()
            os.fsync(csv_file.fileno())
        os.replace(temporary_path, export_path)
    except (OSError, sqlite3.Error, csv.Error) as exc:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise PaperTradeExportError(
            f"The SQLite journal is authoritative, but its CSV export "
            f"could not be updated at {export_path}"
        ) from exc
    return export_path


def save_daily_copy(database_path=None, history_dir=None, today=None):
    """Copy every trade to TRADE_HISTORY_DIR/paper_trades_YYYY-MM-DD.csv.

    One file per day; earlier days are never changed, so the trade record
    survives even if the journal is damaged later. Today's file is rewritten
    by each run of the day. Returns the path.
    """
    from datetime import date

    if history_dir is None:
        journal = _database_path(database_path)
        # A test or other journal keeps its copies next to itself.
        history_dir = (
            TRADE_HISTORY_DIR if journal == DEFAULT_JOURNAL_PATH
            else journal.parent / "trade_history"
        )
    history_dir = Path(history_dir)
    day = today or date.today()
    path = history_dir / f"paper_trades_{day.isoformat()}.csv"
    return export_paper_trades_csv(database_path, path)


def record_entry(
    ticker,
    entry_price,
    atr,
    shares,
    stop_price,
    target_price,
    setup_score=None,
    entry_time=None,
    database_path=None,
    strategy=None,
    entry_source=None,
    broker_order_id=None,
    target2_price=None,
    sector=None,
    entry_note=None,
):
    ticker = str(ticker).strip().upper()
    if not ticker:
        raise ValueError("Ticker is required")

    entry_price = _validate_positive("Entry price", entry_price)
    atr = _validate_positive("ATR", atr)
    stop_price = _validate_positive("Stop price", stop_price)
    target_price = _validate_positive("Target price", target_price)
    if stop_price >= entry_price:
        raise ValueError("Stop price must be below entry price")
    if target_price <= entry_price:
        raise ValueError("Target price must be above entry price")
    if target2_price is not None:
        target2_price = _validate_positive("Second target price", target2_price)
        if target2_price <= entry_price:
            raise ValueError("Second target price must be above entry price")

    try:
        shares = int(shares)
    except (TypeError, ValueError) as exc:
        raise ValueError("Shares must be a positive integer") from exc
    if shares < 1:
        raise ValueError("Shares must be a positive integer")
    if setup_score is not None:
        setup_score = _validate_positive("Setup score", setup_score)

    received_at = time.time()
    initialize_journal(database_path)
    with _connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _claim_signal(connection, ticker, "BUY", received_at)

        open_trade = connection.execute(
            "SELECT id FROM paper_trades WHERE ticker = ? AND status = 'OPEN'",
            (ticker,),
        ).fetchone()
        if open_trade:
            raise OpenTradeExistsError(
                f"An open paper trade already exists for {ticker}"
            )

        cursor = connection.execute(
            """
            INSERT INTO paper_trades (
                ticker, entry_time, entry_price, atr, shares, stop_price,
                target_price, setup_score, status, strategy,
                entry_source, broker_order_id, target2_price, sector,
                entry_note
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, ?)
            """,
            (
                ticker,
                entry_time or time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ",
                    time.gmtime(received_at),
                ),
                entry_price,
                atr,
                shares,
                stop_price,
                target_price,
                float(setup_score) if setup_score is not None else None,
                strategy,
                entry_source,
                broker_order_id,
                target2_price,
                sector or None,
                entry_note or None,
            ),
        )
        trade = dict(connection.execute(
            "SELECT * FROM paper_trades WHERE id = ?",
            (cursor.lastrowid,),
        ).fetchone())
    try:
        export_paper_trades_csv(database_path)
    except PaperTradeExportError:
        raise
    return trade


def record_exit(ticker, exit_price, exit_time=None, database_path=None):
    ticker = str(ticker).strip().upper()
    if not ticker:
        raise ValueError("Ticker is required")
    exit_price = _validate_positive("Exit price", exit_price)
    received_at = time.time()

    initialize_journal(database_path)
    with _connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        trade = connection.execute(
            """
            SELECT * FROM paper_trades
            WHERE ticker = ? AND status = 'OPEN'
            ORDER BY id DESC LIMIT 1
            """,
            (ticker,),
        ).fetchone()
        if trade is None:
            return None

        _claim_signal(connection, ticker, "SELL", received_at)
        realized_pnl = _realized_pnl(trade, exit_price)
        closed_at = exit_time or time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(received_at),
        )
        connection.execute(
            """
            UPDATE paper_trades
            SET status = 'CLOSED', exit_time = ?, exit_price = ?,
                realized_pnl = ?, exit_reason = 'TRADINGVIEW_SELL'
            WHERE id = ? AND status = 'OPEN'
            """,
            (closed_at, exit_price, realized_pnl, trade["id"]),
        )
        closed_trade = dict(connection.execute(
            "SELECT * FROM paper_trades WHERE id = ?",
            (trade["id"],),
        ).fetchone())
    try:
        export_paper_trades_csv(database_path)
    except PaperTradeExportError:
        raise
    return closed_trade


def _realized_pnl(trade, exit_price):
    """P&L of the whole trade: the half sold at 2R plus the rest at exit_price."""
    sold = trade["partial_shares"] or 0
    pnl = (exit_price - trade["entry_price"]) * (trade["shares"] - sold)
    if sold:
        pnl += (trade["partial_exit_price"] - trade["entry_price"]) * sold
    return pnl


def record_partial_exit(
    trade_id, shares, exit_price, exit_time, active_stop_price=None, database_path=None
):
    """Record the half sold at the first target; None if not open or already done."""
    exit_price = _validate_positive("Partial exit price", exit_price)
    shares = int(shares)
    initialize_journal(database_path)
    with _connection(database_path) as connection:
        cursor = connection.execute(
            """
            UPDATE paper_trades
            SET partial_shares = ?, partial_exit_price = ?, partial_exit_time = ?,
                active_stop_price = ?
            WHERE id = ? AND status = 'OPEN' AND partial_exit_price IS NULL
                AND ? BETWEEN 1 AND shares - 1
            """,
            (shares, exit_price, exit_time, active_stop_price, trade_id, shares),
        )
        if cursor.rowcount == 0:
            return None
        trade = dict(connection.execute(
            "SELECT * FROM paper_trades WHERE id = ?", (trade_id,)
        ).fetchone())
    export_paper_trades_csv(database_path)
    return trade


def update_trade_orders(
    trade_id, half_order_id=None, rest_order_id=None, active_stop_price=None,
    database_path=None,
):
    """Store an open trade's Alpaca exit order ids or its moved stop."""
    initialize_journal(database_path)
    with _connection(database_path) as connection:
        connection.execute(
            """
            UPDATE paper_trades
            SET half_order_id = COALESCE(?, half_order_id),
                rest_order_id = COALESCE(?, rest_order_id),
                active_stop_price = COALESCE(?, active_stop_price)
            WHERE id = ?
            """,
            (half_order_id, rest_order_id, active_stop_price, trade_id),
        )
        trade = dict(connection.execute(
            "SELECT * FROM paper_trades WHERE id = ?", (trade_id,)
        ).fetchone())
    return trade


def close_trade(trade_id, exit_price, exit_reason, exit_time, database_path=None):
    """Close one open trade by id; None if it is no longer open.

    After a half was sold at the first target, exit_price is the price of
    the remaining shares and the reason gets a PARTIAL+ prefix.
    """
    exit_price = _validate_positive("Exit price", exit_price)
    initialize_journal(database_path)
    with _connection(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        trade = connection.execute(
            "SELECT * FROM paper_trades WHERE id = ? AND status = 'OPEN'",
            (trade_id,),
        ).fetchone()
        if trade is None:
            return None

        realized_pnl = _realized_pnl(trade, exit_price)
        if trade["partial_shares"] and not exit_reason.startswith("PARTIAL+"):
            exit_reason = f"PARTIAL+{exit_reason}"
        connection.execute(
            """
            UPDATE paper_trades
            SET status = 'CLOSED', exit_time = ?, exit_price = ?,
                realized_pnl = ?, exit_reason = ?
            WHERE id = ? AND status = 'OPEN'
            """,
            (exit_time, exit_price, realized_pnl, exit_reason, trade_id),
        )
        closed_trade = dict(connection.execute(
            "SELECT * FROM paper_trades WHERE id = ?",
            (trade_id,),
        ).fetchone())
    export_paper_trades_csv(database_path)
    return closed_trade


def add_broker_order(
    order_id,
    ticker,
    shares,
    atr,
    stop_price,
    target_price,
    strategy=None,
    entry_source=None,
    setup_score=None,
    database_path=None,
    target2_price=None,
    sector=None,
    entry_note=None,
):
    """Remember a sent entry order until it fills or is canceled."""
    initialize_journal(database_path)
    with _connection(database_path) as connection:
        connection.execute(
            """
            INSERT INTO broker_orders (
                order_id, ticker, strategy, entry_source, shares, atr,
                stop_price, target_price, setup_score, status, submitted_at,
                target2_price, sector, entry_note
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?)
            """,
            (
                str(order_id),
                str(ticker).strip().upper(),
                strategy,
                entry_source,
                int(shares),
                float(atr),
                float(stop_price),
                float(target_price),
                setup_score,
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                float(target2_price) if target2_price is not None else None,
                sector or None,
                entry_note or None,
            ),
        )


def held_sectors(database_path=None):
    """{sector: [tickers]} for open trades and entry orders not yet filled.

    Trades with no recorded sector are left out.
    """
    initialize_journal(database_path)
    with _connection(database_path) as connection:
        rows = connection.execute(
            """
            SELECT ticker, sector FROM paper_trades WHERE status = 'OPEN'
            UNION ALL
            SELECT ticker, sector FROM broker_orders WHERE status = 'PENDING'
            """
        ).fetchall()
    sectors = {}
    for row in rows:
        if row["sector"]:
            sectors.setdefault(row["sector"], []).append(row["ticker"])
    return sectors


def get_closed_trades(database_path=None):
    """Closed trades, newest exit first."""
    initialize_journal(database_path)
    with _connection(database_path) as connection:
        rows = connection.execute(
            "SELECT * FROM paper_trades WHERE status = 'CLOSED' "
            "ORDER BY exit_time DESC, id DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def set_trade_note(trade_id, field, text, database_path=None):
    """Save the entry note (why) or exit note (lesson) of one trade."""
    if field not in NOTE_FIELDS:
        raise ValueError(f"Note field must be one of {', '.join(NOTE_FIELDS)}")
    text = str(text or "").strip()
    if len(text) > MAX_NOTE_LENGTH:
        raise ValueError(f"Note is longer than {MAX_NOTE_LENGTH} characters")
    initialize_journal(database_path)
    with _connection(database_path) as connection:
        cursor = connection.execute(
            f"UPDATE paper_trades SET {field} = ? WHERE id = ?",
            (text or None, int(trade_id)),
        )
        if cursor.rowcount == 0:
            raise ValueError(f"No paper trade with id {trade_id}")
        trade = dict(connection.execute(
            "SELECT * FROM paper_trades WHERE id = ?", (int(trade_id),)
        ).fetchone())
    try:
        export_paper_trades_csv(database_path)
    except PaperTradeExportError:
        pass
    return trade


def get_pending_broker_orders(database_path=None):
    initialize_journal(database_path)
    with _connection(database_path) as connection:
        rows = connection.execute(
            "SELECT * FROM broker_orders WHERE status = 'PENDING' "
            "ORDER BY submitted_at"
        ).fetchall()
    return [dict(row) for row in rows]


def finish_broker_order(order_id, status, trade_id=None, detail=None, database_path=None):
    initialize_journal(database_path)
    with _connection(database_path) as connection:
        connection.execute(
            """
            UPDATE broker_orders SET status = ?, trade_id = ?, detail = ?
            WHERE order_id = ? AND status = 'PENDING'
            """,
            (status, trade_id, detail, str(order_id)),
        )


def count_open_trades(database_path=None):
    initialize_journal(database_path)
    with _connection(database_path) as connection:
        return connection.execute(
            "SELECT COUNT(*) FROM paper_trades WHERE status = 'OPEN'"
        ).fetchone()[0]


def get_paper_trades(limit=25, database_path=None):
    initialize_journal(database_path)
    export_path = (
        PAPER_TRADES_CSV
        if _database_path(database_path) == DEFAULT_JOURNAL_PATH
        else _database_path(database_path).with_suffix(".csv")
    )
    if not export_path.exists():
        export_paper_trades_csv(database_path)
    with _connection(database_path) as connection:
        open_trades = connection.execute(
            """
            SELECT * FROM paper_trades
            WHERE status = 'OPEN'
            ORDER BY entry_time DESC, id DESC
            """
        ).fetchall()
        recent_trades = connection.execute(
            """
            SELECT * FROM paper_trades
            ORDER BY id DESC
            LIMIT ?
            """,
            (max(1, min(int(limit), 100)),),
        ).fetchall()
        summary = connection.execute(
            """
            SELECT
                COUNT(*) AS total_trades,
                SUM(CASE WHEN status = 'OPEN' THEN 1 ELSE 0 END) AS open_trades,
                SUM(CASE WHEN status = 'CLOSED' THEN 1 ELSE 0 END) AS closed_trades,
                SUM(CASE WHEN status = 'CLOSED' THEN realized_pnl ELSE 0 END) AS realized_pnl,
                SUM(CASE WHEN status = 'CLOSED' AND realized_pnl > 0 THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN status = 'CLOSED' AND realized_pnl < 0 THEN 1 ELSE 0 END) AS losses,
                SUM(CASE WHEN status = 'OPEN' THEN
                    (entry_price - COALESCE(active_stop_price, stop_price))
                    * (shares - COALESCE(partial_shares, 0))
                    ELSE 0 END) AS open_risk_at_stop
            FROM paper_trades
            """
        ).fetchone()
        strategy_rows = connection.execute(
            """
            SELECT
                COALESCE(strategy, 'UNKNOWN') AS strategy,
                COUNT(*) AS total_trades,
                SUM(CASE WHEN status = 'OPEN' THEN 1 ELSE 0 END) AS open_trades,
                SUM(CASE WHEN status = 'CLOSED' THEN 1 ELSE 0 END) AS closed_trades,
                SUM(CASE WHEN status = 'CLOSED' THEN realized_pnl ELSE 0 END) AS realized_pnl,
                SUM(CASE WHEN status = 'CLOSED' AND realized_pnl > 0 THEN 1 ELSE 0 END) AS wins
            FROM paper_trades
            GROUP BY COALESCE(strategy, 'UNKNOWN')
            """
        ).fetchall()
    by_strategy = {}
    for row in strategy_rows:
        stats = {key: value or 0 for key, value in dict(row).items()}
        stats["win_rate"] = (
            stats["wins"] / stats["closed_trades"] * 100
            if stats["closed_trades"]
            else None
        )
        by_strategy[stats.pop("strategy")] = stats
    summary = dict(summary)
    summary = {
        key: value or 0
        for key, value in summary.items()
    }
    summary["win_rate"] = (
        summary["wins"] / summary["closed_trades"] * 100
        if summary["closed_trades"]
        else None
    )
    open_trades = [dict(trade) for trade in open_trades]
    for trade in open_trades:
        trade["shares_open"] = trade["shares"] - (trade["partial_shares"] or 0)
        trade["risk_at_stop"] = (
            trade["entry_price"]
            - (trade["active_stop_price"] or trade["stop_price"])
        ) * trade["shares_open"]
    return {
        "open": open_trades,
        "recent": [dict(trade) for trade in recent_trades],
        "summary": summary,
        "by_strategy": by_strategy,
    }
