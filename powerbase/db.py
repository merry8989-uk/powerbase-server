"""SQLite access layer.

Design notes
------------
* One connection per thread (``threading.local``), because FastAPI runs sync
  endpoints in a thread pool.
* ``journal_mode=WAL`` so readers never block the writer.
* A single process-wide write lock plus ``BEGIN IMMEDIATE`` guarantees exactly
  one writer at a time — the only way to avoid ``database is locked`` errors
  under concurrency with SQLite.
* ``isolation_level=None`` puts the driver in autocommit mode so *we* control
  transaction boundaries. This matters because the legacy driver only opens
  transactions implicitly for DML, not for DDL — which would break atomic
  migrations.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class QueryTimeout(Exception):
    """Raised when a read query exceeds its wall-clock budget."""

    def __init__(self, timeout_ms: int):
        super().__init__(f"query exceeded {timeout_ms} ms")
        self.timeout_ms = timeout_ms


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._local = threading.local()
        self._write_lock = threading.Lock()
        if self.path != ":memory:":
            Path(self.path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)

    # -- connections ------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            timeout=30.0,
            isolation_level=None,          # autocommit; we manage BEGIN/COMMIT
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")

        # WAL is a persistent property of the *file*, not of the connection.
        # Issuing `PRAGMA journal_mode=WAL` unconditionally on every new
        # connection makes each one take a lock on the database — under
        # concurrency, new threadpool threads then block behind the current
        # writer, which measured as a 10x slowdown on the ingest path. Read it
        # (cheap, no exclusive lock) and set it only when it is not already WAL.
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        if str(mode).lower() != "wal":
            conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def journal_mode(self) -> str:
        return str(self.conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()

    @property
    def conn(self) -> sqlite3.Connection:
        """The calling thread's connection, created on first use."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    # -- transactions -----------------------------------------------------
    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """Serialised, atomic write transaction."""
        with self._write_lock:
            conn = self.conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    @contextmanager
    def exclusive(self) -> Iterator[sqlite3.Connection]:
        """Hold the write lock *without* opening a transaction.

        Needed by operations SQLite refuses to run inside one — `VACUUM` and the
        online backup API. Taking the lock still guarantees no writer is midway
        through a batch while we copy or compact the file.
        """
        with self._write_lock:
            yield self.conn

    def vacuum(self) -> None:
        """Reclaim space after pruning. Cannot run inside a transaction."""
        with self._write_lock:
            self.conn.execute("VACUUM")
            self.conn.execute("PRAGMA optimize")

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # -- helpers ----------------------------------------------------------
    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, params))

    def one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def scalar(self, sql: str, params: tuple = ()):
        row = self.conn.execute(sql, params).fetchone()
        return None if row is None else row[0]

    def query_timed(
        self, sql: str, params: tuple = (), timeout_ms: int = 5000, op_limit: int = 2000
    ) -> list[sqlite3.Row]:
        """Run a read query with a real wall-clock budget.

        SQLite has no statement timeout, but it does let us install a progress
        handler that is called every N virtual-machine steps, and returning
        non-zero aborts the statement. That makes this an actual timeout rather
        than a heuristic — an expensive query is stopped, not merely measured
        after the fact.

        The handler is always cleared, because the connection is per-thread and
        reused: leaving one installed would abort unrelated later queries.
        """
        conn = self.conn
        deadline = time.monotonic() + (timeout_ms / 1000.0)

        def expired() -> int:
            return 1 if time.monotonic() > deadline else 0

        conn.set_progress_handler(expired, op_limit)
        try:
            return list(conn.execute(sql, params))
        except sqlite3.OperationalError as exc:
            if "interrupt" in str(exc).lower():
                raise QueryTimeout(timeout_ms) from exc
            raise
        finally:
            conn.set_progress_handler(None, 0)
