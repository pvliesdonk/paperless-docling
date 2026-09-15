from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5_000

_MIGRATION_V1 = (
    """
    CREATE TABLE jobs (
        id INTEGER PRIMARY KEY,
        document_id INTEGER NOT NULL CHECK (document_id > 0),
        event TEXT NOT NULL,
        state TEXT NOT NULL CHECK (
            state IN (
                'pending', 'leased', 'retry_wait', 'completed',
                'superseded', 'failed', 'cancelled'
            )
        ),
        force INTEGER NOT NULL DEFAULT 0 CHECK (force IN (0, 1)),
        request_id TEXT,
        content_hash TEXT,
        profile_version TEXT NOT NULL,
        attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
        cumulative_attempts INTEGER NOT NULL DEFAULT 0
            CHECK (cumulative_attempts >= 0),
        retry_epoch INTEGER NOT NULL DEFAULT 0 CHECK (retry_epoch >= 0),
        next_attempt_at TEXT,
        lease_owner TEXT,
        lease_token TEXT,
        lease_expires_at TEXT,
        last_delivery_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        started_at TEXT,
        completed_at TEXT,
        last_error_class TEXT,
        last_error_message TEXT,
        error_history TEXT NOT NULL DEFAULT '[]',
        model_name TEXT,
        input_tokens INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
        output_tokens INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
        latency_ms INTEGER CHECK (latency_ms IS NULL OR latency_ms >= 0),
        CHECK (
            (force = 0 AND request_id IS NULL)
            OR (force = 1 AND request_id IS NOT NULL)
        )
    )
    """,
    """
    CREATE INDEX idx_jobs_claimable
    ON jobs (state, next_attempt_at, lease_expires_at, created_at, id)
    """,
    """
    CREATE INDEX idx_jobs_document_state
    ON jobs (document_id, state)
    """,
    """
    CREATE UNIQUE INDEX uq_jobs_unresolved_normal
    ON jobs (document_id, profile_version)
    WHERE force = 0
      AND content_hash IS NULL
      AND state IN ('pending', 'retry_wait')
    """,
    """
    CREATE UNIQUE INDEX uq_jobs_forced_request_id
    ON jobs (request_id)
    WHERE request_id IS NOT NULL
    """,
    """
    CREATE UNIQUE INDEX uq_jobs_completed_normal_identity
    ON jobs (document_id, content_hash, profile_version)
    WHERE force = 0 AND state = 'completed'
    """,
)


class UnsupportedSchemaError(RuntimeError):
    """Raised when the database was created by a newer worker."""


class Database:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self._transaction_lock = threading.RLock()

    @classmethod
    def open(cls, path: Path) -> Database:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            path,
            isolation_level=None,
            check_same_thread=False,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        except BaseException:
            connection.close()
            raise
        return cls(connection)

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        with self._transaction_lock:
            self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield self.connection
            except BaseException:
                self.connection.rollback()
                raise
            else:
                try:
                    self.connection.commit()
                except BaseException:
                    self.connection.rollback()
                    raise

    def migrate(self) -> None:
        with self.transaction(immediate=True) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise UnsupportedSchemaError(
                    f"Database schema version {version} is newer than supported "
                    f"version {SCHEMA_VERSION}."
                )
            if version == SCHEMA_VERSION:
                return

            for statement in _MIGRATION_V1:
                connection.execute(statement)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def check_ready(self) -> None:
        """Verify the main database accepts writes at the current schema."""
        with self.transaction(immediate=True) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version != SCHEMA_VERSION:
                raise UnsupportedSchemaError("Database schema is not current.")
            connection.execute(
                "UPDATE jobs SET updated_at = updated_at WHERE id = -1"
            )

    def close(self) -> None:
        self.connection.close()
