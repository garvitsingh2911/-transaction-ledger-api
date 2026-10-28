import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
BUSY_TIMEOUT_MS = 5000  # how long a writer waits for the lock before giving up (503)


def db_path() -> str:
    return os.environ.get("LEDGER_DB_PATH", "ledger.db")


def connect() -> sqlite3.Connection:
    # isolation_level=None: we issue BEGIN/COMMIT ourselves so every write
    # path controls exactly where its atomic unit starts and ends.
    conn = sqlite3.connect(db_path(), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


def init_db() -> None:
    # Several workers start at once on the same file. Switching to WAL ignores
    # busy_timeout, so a racing worker gets "database is locked" immediately.
    # Every statement here is idempotent, so just retry for up to ~5s.
    for attempt in range(50):
        conn = connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")  # readers never block the writer
            conn.executescript(SCHEMA_PATH.read_text())
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or attempt == 49:
                raise
        finally:
            conn.close()
        time.sleep(0.1)


def get_conn() -> Iterator[sqlite3.Connection]:
    """FastAPI dependency: one short-lived connection per request."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def atomic(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """All-or-nothing write unit.

    BEGIN IMMEDIATE takes the write lock up front, so the read-check-write
    sequence inside (balance check, duplicate check) cannot interleave with
    another writer: no lost updates, no double spends. Any exception rolls
    everything back.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        # Some SQLite errors (e.g. SQLITE_FULL) already roll back; a second
        # ROLLBACK would raise and hide the original error.
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


@contextmanager
def snapshot(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Read-only unit: every SELECT inside sees the same committed state.

    In WAL mode a deferred BEGIN takes no write lock, so writers are never blocked.
    """
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        if conn.in_transaction:
            conn.execute("COMMIT")
