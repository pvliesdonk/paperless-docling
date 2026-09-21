from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from ._job_codec import (
    claimed_job_from_row,
    from_text,
    job_from_row,
    load_error_history,
    require_changed,
    require_utc,
    safe_error,
    to_text,
)
from ._job_types import (
    ClaimedJob,
    EnqueueConflictError,
    Job,
    JobError,
    JobMetrics,
    JobState,
)
from .config import WorkerConfig
from .db import Database


def enqueue(
    database: Database,
    config: WorkerConfig,
    clock: Callable[[], datetime],
    document_id: int,
    event: str,
    *,
    force: bool = False,
    request_id: UUID | None = None,
) -> Job:
    _validate_enqueue(document_id, event, force, request_id)
    now = require_utc(clock())
    request_text = str(request_id) if request_id is not None else None
    with database.transaction(immediate=True) as connection:
        existing = _existing_for_request(
            connection, request_text, document_id, event
        )
        if existing is not None:
            return existing
        if not force:
            coalesced = _coalesce_normal(
                connection, config.profile_version, document_id, event, now
            )
            if coalesced is not None:
                return coalesced
        return _insert_job(
            connection,
            config.profile_version,
            document_id,
            event,
            force,
            request_text,
            now,
        )


def _validate_enqueue(
    document_id: int,
    event: str,
    force: bool,
    request_id: UUID | None,
) -> None:
    if isinstance(document_id, bool) or not isinstance(document_id, int):
        raise ValueError("document_id must be a positive integer")
    if document_id <= 0:
        raise ValueError("document_id must be a positive integer")
    if event not in {"document_added", "document_updated"}:
        raise ValueError("event must be document_added or document_updated")
    if not isinstance(force, bool):
        raise ValueError("force must be boolean")
    if force and not isinstance(request_id, UUID):
        raise ValueError("forced enqueue requires a UUID request_id")
    if not force and request_id is not None:
        raise ValueError("normal enqueue rejects request_id")


def _existing_for_request(
    connection: sqlite3.Connection,
    request_text: str | None,
    document_id: int,
    event: str,
) -> Job | None:
    if request_text is None:
        return None
    row = connection.execute(
        "SELECT * FROM jobs WHERE request_id = ?", (request_text,)
    ).fetchone()
    if row is None:
        return None
    if row["document_id"] != document_id or row["event"] != event:
        raise EnqueueConflictError("request_id is already bound to another payload")
    return job_from_row(row)


def _coalesce_normal(
    connection: sqlite3.Connection,
    profile_version: str,
    document_id: int,
    event: str,
    now: datetime,
) -> Job | None:
    existing = connection.execute(
        """
        SELECT * FROM jobs
        WHERE document_id = ? AND profile_version = ?
          AND force = 0 AND content_hash IS NULL
          AND state IN ('pending', 'retry_wait')
        """,
        (document_id, profile_version),
    ).fetchone()
    if existing is None:
        return None
    now_text = to_text(now)
    connection.execute(
        """
        UPDATE jobs SET event = ?, state = 'pending',
            next_attempt_at = NULL, last_delivery_at = ?,
            updated_at = ? WHERE id = ?
        """,
        (event, now_text, now_text, existing["id"]),
    )
    row = connection.execute(
        "SELECT * FROM jobs WHERE id = ?", (existing["id"],)
    ).fetchone()
    return job_from_row(row)


def _insert_job(
    connection: sqlite3.Connection,
    profile_version: str,
    document_id: int,
    event: str,
    force: bool,
    request_text: str | None,
    now: datetime,
) -> Job:
    now_text = to_text(now)
    cursor = connection.execute(
        """
        INSERT INTO jobs (
            document_id, event, state, force, request_id, profile_version,
            last_delivery_at, created_at, updated_at
        ) VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?)
        """,
        (
            document_id,
            event,
            int(force),
            request_text,
            profile_version,
            now_text,
            now_text,
            now_text,
        ),
    )
    row = connection.execute(
        "SELECT * FROM jobs WHERE id = ?", (cursor.lastrowid,)
    ).fetchone()
    return job_from_row(row)


def metrics_snapshot(
    database: Database,
    config: WorkerConfig,
    clock: Callable[[], datetime],
) -> JobMetrics:
    now = require_utc(clock())
    now_text = to_text(now)
    state_totals = {state: 0 for state in JobState}
    with database.transaction() as connection:
        rows = connection.execute(
            "SELECT state, count(*) AS total FROM jobs GROUP BY state"
        ).fetchall()
        for row in rows:
            state_totals[JobState(row["state"])] = row["total"]
        eligible = connection.execute(
            """
            SELECT count(*) AS depth, min(created_at) AS oldest
            FROM jobs
            WHERE state = 'pending'
               OR (state = 'retry_wait' AND next_attempt_at <= ?)
               OR (
                    state = 'leased' AND lease_expires_at <= ?
                    AND attempt_count < ?
               )
            """,
            (now_text, now_text, config.max_attempts),
        ).fetchone()
    oldest = from_text(eligible["oldest"])
    age = max(0.0, (now - oldest).total_seconds()) if oldest else 0.0
    return JobMetrics(state_totals, eligible["depth"], age)


def claim(
    database: Database,
    config: WorkerConfig,
    worker_id: str,
    now: datetime,
) -> ClaimedJob | None:
    if not isinstance(worker_id, str) or not worker_id:
        raise ValueError("worker_id must be non-empty")
    now = require_utc(now)
    now_text = to_text(now)
    lease_expires_at = now + timedelta(seconds=config.lease_seconds)
    token = str(uuid4())
    with database.transaction(immediate=True) as connection:
        row = _next_claimable(connection, config.max_attempts, now, now_text)
        if row is None:
            return None
        connection.execute(
            """
            UPDATE jobs
            SET state = 'leased', lease_owner = ?, lease_token = ?,
                lease_expires_at = ?, updated_at = ?,
                started_at = COALESCE(started_at, ?), next_attempt_at = NULL,
                attempt_count = attempt_count + 1,
                cumulative_attempts = cumulative_attempts + 1
            WHERE id = ?
            """,
            (
                worker_id,
                token,
                to_text(lease_expires_at),
                now_text,
                now_text,
                row["id"],
            ),
        )
        claimed = connection.execute(
            "SELECT * FROM jobs WHERE id = ?", (row["id"],)
        ).fetchone()
        return claimed_job_from_row(claimed)


def _next_claimable(
    connection: sqlite3.Connection,
    max_attempts: int,
    now: datetime,
    now_text: str,
) -> sqlite3.Row | None:
    while True:
        row = connection.execute(
            """
            SELECT * FROM jobs
            WHERE (state = 'pending')
               OR (state = 'retry_wait' AND next_attempt_at <= ?)
               OR (state = 'leased' AND lease_expires_at <= ?)
            ORDER BY created_at, id
            LIMIT 1
            """,
            (now_text, now_text),
        ).fetchone()
        if row is None:
            return None
        if row["state"] != JobState.LEASED.value or row["attempt_count"] < max_attempts:
            return row
        _fail_exhausted_lease(connection, row, now)


def _fail_exhausted_lease(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    now: datetime,
) -> None:
    error_class, error_message, record = safe_error(
        JobError(
            "LeaseExpired",
            "lease expired after maximum attempts",
            code="lease-expired",
        ),
        now,
    )
    history = (*load_error_history(row["error_history"]), record)
    cursor = connection.execute(
        """
        UPDATE jobs
        SET state = 'failed', updated_at = ?, completed_at = ?,
            lease_owner = NULL, lease_token = NULL,
            lease_expires_at = NULL, last_error_class = ?,
            last_error_message = ?, error_history = ?
        WHERE id = ? AND state = 'leased' AND lease_token = ?
          AND lease_expires_at <= ? AND attempt_count >= ?
        """,
        (
            to_text(now),
            to_text(now),
            error_class,
            error_message,
            json.dumps(history),
            row["id"],
            row["lease_token"],
            to_text(now),
            row["attempt_count"],
        ),
    )
    require_changed(cursor)
