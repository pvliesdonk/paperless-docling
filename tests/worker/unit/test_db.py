import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from paperless_docling_worker.db import Database, UnsupportedSchemaError

EXPECTED_JOB_COLUMNS = {
    "id",
    "document_id",
    "event",
    "state",
    "force",
    "request_id",
    "content_hash",
    "profile_version",
    "attempt_count",
    "cumulative_attempts",
    "retry_epoch",
    "next_attempt_at",
    "lease_owner",
    "lease_token",
    "lease_expires_at",
    "last_delivery_at",
    "created_at",
    "updated_at",
    "started_at",
    "completed_at",
    "last_error_class",
    "last_error_message",
    "error_history",
    "model_name",
    "input_tokens",
    "output_tokens",
    "latency_ms",
}

EXPECTED_JOB_INDEXES = {
    "idx_jobs_claimable",
    "idx_jobs_document_state",
    "uq_jobs_unresolved_normal",
    "uq_jobs_forced_request_id",
    "uq_jobs_completed_normal_identity",
}

EXPECTED_UNIQUE_INDEX_SQL = {
    "uq_jobs_unresolved_normal": (
        "CREATE UNIQUE INDEX uq_jobs_unresolved_normal "
        "ON jobs (document_id, profile_version) "
        "WHERE force = 0 AND content_hash IS NULL "
        "AND state IN ('pending', 'retry_wait')"
    ),
    "uq_jobs_forced_request_id": (
        "CREATE UNIQUE INDEX uq_jobs_forced_request_id "
        "ON jobs (request_id) WHERE request_id IS NOT NULL"
    ),
    "uq_jobs_completed_normal_identity": (
        "CREATE UNIQUE INDEX uq_jobs_completed_normal_identity "
        "ON jobs (document_id, content_hash, profile_version) "
        "WHERE force = 0 AND state = 'completed'"
    ),
}


def test_open_configures_every_connection_for_safe_concurrent_access(tmp_path):
    path = tmp_path / "state" / "summary.sqlite3"
    first = Database.open(path)
    second = Database.open(path)
    try:
        assert first.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert second.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert first.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert second.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert first.connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5_000
        assert second.connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5_000
    finally:
        first.close()
        second.close()


def test_transaction_commits_success_and_rolls_back_failure(tmp_path):
    database = Database.open(tmp_path / "summary.sqlite3")
    try:
        database.connection.execute("CREATE TABLE values_table (value TEXT NOT NULL)")

        with database.transaction() as connection:
            connection.execute("INSERT INTO values_table (value) VALUES (?)", ("kept",))

        with pytest.raises(RuntimeError, match="abort"):
            with database.transaction() as connection:
                connection.execute(
                    "INSERT INTO values_table (value) VALUES (?)", ("discarded",)
                )
                raise RuntimeError("abort")

        values = database.connection.execute(
            "SELECT value FROM values_table ORDER BY value"
        ).fetchall()
        assert [row[0] for row in values] == ["kept"]
    finally:
        database.close()


def test_migration_creates_versioned_task_two_schema_and_indexes(tmp_path):
    database = Database.open(tmp_path / "summary.sqlite3")
    try:
        database.migrate()

        assert database.connection.execute("PRAGMA user_version").fetchone()[0] == 1
        table_names = {
            row[0]
            for row in database.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = ?", ("table",)
            )
        }
        assert "jobs" in table_names

        columns = {
            row[1] for row in database.connection.execute("PRAGMA table_info(jobs)")
        }
        assert columns == EXPECTED_JOB_COLUMNS

        indexes = {
            row[1] for row in database.connection.execute("PRAGMA index_list(jobs)")
        }
        assert EXPECTED_JOB_INDEXES <= indexes

        index_sql = {
            row[0]: " ".join(row[1].split())
            for row in database.connection.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type = ? AND name IN (?, ?, ?)",
                (
                    "index",
                    "uq_jobs_unresolved_normal",
                    "uq_jobs_forced_request_id",
                    "uq_jobs_completed_normal_identity",
                ),
            )
        }
        assert index_sql == EXPECTED_UNIQUE_INDEX_SQL
    finally:
        database.close()


def test_migration_is_idempotent(tmp_path):
    database = Database.open(tmp_path / "summary.sqlite3")
    try:
        database.migrate()
        first_schema = database.connection.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE name NOT LIKE ? ORDER BY type, name",
            ("sqlite_%",),
        ).fetchall()

        database.migrate()

        second_schema = database.connection.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE name NOT LIKE ? ORDER BY type, name",
            ("sqlite_%",),
        ).fetchall()
        assert second_schema == first_schema
        assert database.connection.execute("PRAGMA user_version").fetchone()[0] == 1
    finally:
        database.close()


def test_concurrent_initializers_serialize_before_reading_schema_version(tmp_path):
    version_reads = threading.Barrier(2)

    class SynchronizedConnection(sqlite3.Connection):
        def execute(self, sql, parameters=(), /):
            cursor = super().execute(sql, parameters)
            if (
                sql.strip() == "PRAGMA user_version"
                and not self.in_transaction
                and not getattr(self, "version_read_synchronized", False)
            ):
                self.version_read_synchronized = True
                version_reads.wait(timeout=5)
            return cursor

    path = tmp_path / "summary.sqlite3"
    databases = [
        Database(
            sqlite3.connect(
                path,
                timeout=5,
                isolation_level=None,
                check_same_thread=False,
                factory=SynchronizedConnection,
            )
        )
        for _ in range(2)
    ]
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(database.migrate) for database in databases]
            for future in futures:
                future.result(timeout=10)

        with sqlite3.connect(path) as verification:
            assert verification.execute("PRAGMA user_version").fetchone()[0] == 1
            jobs_tables = verification.execute(
                "SELECT count(*) FROM sqlite_master WHERE type = ? AND name = ?",
                ("table", "jobs"),
            ).fetchone()[0]
            assert jobs_tables == 1
    finally:
        for database in databases:
            database.close()


def test_migration_rejects_a_schema_newer_than_supported(tmp_path):
    path = tmp_path / "summary.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 2")
    connection.close()
    database = Database.open(path)
    try:
        with pytest.raises(UnsupportedSchemaError, match="version 2"):
            database.migrate()

        assert database.connection.execute("PRAGMA user_version").fetchone()[0] == 2
    finally:
        database.close()


def insert_job(
    database: Database,
    *,
    document_id: int = 1,
    state: str = "pending",
    force: int = 0,
    request_id: str | None = None,
    content_hash: str | None = None,
    profile_version: str = "profile-v1",
) -> None:
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO jobs (
                document_id, event, state, force, request_id, content_hash,
                profile_version, last_delivery_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                document_id,
                "document_added",
                state,
                force,
                request_id,
                content_hash,
                profile_version,
                "2026-08-27T12:00:00+00:00",
                "2026-08-27T12:00:00+00:00",
                "2026-08-27T12:00:00+00:00",
            ),
        )


def test_schema_enforces_forced_request_identity(tmp_path):
    database = Database.open(tmp_path / "summary.sqlite3")
    try:
        database.migrate()

        with pytest.raises(sqlite3.IntegrityError):
            insert_job(database, force=1, request_id=None)

        insert_job(database, force=1, request_id="request-1")
        with pytest.raises(sqlite3.IntegrityError):
            insert_job(database, document_id=2, force=1, request_id="request-1")
    finally:
        database.close()


def test_schema_enforces_completed_normal_identity_only(tmp_path):
    database = Database.open(tmp_path / "summary.sqlite3")
    try:
        database.migrate()
        insert_job(database, state="completed", content_hash="hash-1")

        with pytest.raises(sqlite3.IntegrityError):
            insert_job(database, state="completed", content_hash="hash-1")

        insert_job(
            database,
            state="completed",
            force=1,
            request_id="request-1",
            content_hash="hash-1",
        )
        insert_job(
            database,
            state="completed",
            force=1,
            request_id="request-2",
            content_hash="hash-1",
        )
    finally:
        database.close()


@pytest.mark.parametrize("state", ["pending", "retry_wait"])
def test_schema_coalesces_pending_and_retry_wait_normal_jobs(tmp_path, state):
    database = Database.open(tmp_path / "summary.sqlite3")
    try:
        database.migrate()
        insert_job(database, state=state)

        with pytest.raises(sqlite3.IntegrityError):
            insert_job(database)

    finally:
        database.close()


def test_schema_allows_pending_successor_while_normal_job_is_leased(tmp_path):
    database = Database.open(tmp_path / "summary.sqlite3")
    try:
        database.migrate()
        insert_job(database, state="leased")

        insert_job(database, state="pending")
    finally:
        database.close()


def test_schema_does_not_coalesce_terminal_or_forced_jobs(tmp_path):
    database = Database.open(tmp_path / "summary.sqlite3")
    try:
        database.migrate()

        insert_job(database, state="failed")
        insert_job(database, force=1, request_id="request-1")
    finally:
        database.close()
