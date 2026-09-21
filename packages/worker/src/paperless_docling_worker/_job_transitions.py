from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from ._job_codec import (
    claimed_job_from_row,
    job_from_row,
    leased_job,
    load_error_history,
    require_changed,
    require_utc,
    safe_error,
    to_text,
)
from ._job_types import (
    BindDisposition,
    BindResult,
    ClaimedJob,
    Job,
    JobError,
    JobState,
    ManualRetryConflictError,
)
from .config import WorkerConfig
from .db import Database


@dataclass(frozen=True)
class TerminalUpdate:
    state: JobState
    at: datetime
    error: JobError | None = None
    model_name: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int | None = None


def bind_content(
    database: Database,
    clock: Callable[[], datetime],
    claim: ClaimedJob,
    content_hash: str,
    profile_version: str,
    observed_at: datetime,
) -> BindResult:
    _validate_content_identity(content_hash, profile_version)
    observed_at = require_utc(observed_at)
    with database.transaction(immediate=True) as connection:
        now_text = to_text(clock())
        durable = leased_job(connection, claim, now_text)
        duplicate = _completed_duplicate(
            connection, durable, content_hash, profile_version
        )
        if duplicate is not None:
            return _supersede_duplicate(
                connection,
                durable,
                duplicate["id"],
                content_hash,
                profile_version,
                to_text(observed_at),
                now_text,
            )
        return _bind_identity(
            connection,
            durable,
            content_hash,
            profile_version,
            to_text(observed_at),
            now_text,
        )


def _validate_content_identity(content_hash: str, profile_version: str) -> None:
    if not isinstance(content_hash, str) or not content_hash:
        raise ValueError("content_hash must be non-empty")
    if not isinstance(profile_version, str) or not profile_version:
        raise ValueError("profile_version must be non-empty")


def _completed_duplicate(
    connection: sqlite3.Connection,
    claim: ClaimedJob,
    content_hash: str,
    profile_version: str,
) -> sqlite3.Row | None:
    if claim.force:
        return None
    return connection.execute(
        """
        SELECT id FROM jobs
        WHERE document_id = ? AND content_hash = ?
          AND profile_version = ? AND force = 0
          AND state = 'completed'
        ORDER BY id LIMIT 1
        """,
        (claim.document_id, content_hash, profile_version),
    ).fetchone()


def _supersede_duplicate(
    connection: sqlite3.Connection,
    claim: ClaimedJob,
    duplicate_id: int,
    content_hash: str,
    profile_version: str,
    observed_at_text: str,
    now_text: str,
) -> BindResult:
    cursor = connection.execute(
        """
        UPDATE jobs
        SET state = 'superseded', content_hash = ?, profile_version = ?,
            updated_at = ?, completed_at = ?, lease_owner = NULL,
            lease_token = NULL, lease_expires_at = NULL
        WHERE id = ? AND state = 'leased' AND lease_token = ?
          AND lease_expires_at > ?
        """,
        (
            content_hash,
            profile_version,
            now_text,
            now_text,
            claim.id,
            claim.lease_token,
            now_text,
        ),
    )
    require_changed(cursor)
    superseded_ids = _supersede_pending_successors(
        connection, claim, profile_version, observed_at_text, now_text
    )
    row = connection.execute(
        "SELECT * FROM jobs WHERE id = ?", (claim.id,)
    ).fetchone()
    return BindResult(
        BindDisposition.ALREADY_COMPLETED,
        job_from_row(row),
        duplicate_of=duplicate_id,
        superseded_job_ids=superseded_ids,
    )


def _bind_identity(
    connection: sqlite3.Connection,
    claim: ClaimedJob,
    content_hash: str,
    profile_version: str,
    observed_at_text: str,
    now_text: str,
) -> BindResult:
    cursor = connection.execute(
        """
        UPDATE jobs
        SET content_hash = ?, profile_version = ?, updated_at = ?
        WHERE id = ? AND state = 'leased' AND lease_token = ?
          AND lease_expires_at > ?
        """,
        (
            content_hash,
            profile_version,
            now_text,
            claim.id,
            claim.lease_token,
            now_text,
        ),
    )
    require_changed(cursor)
    superseded_ids = _supersede_pending_successors(
        connection, claim, profile_version, observed_at_text, now_text
    )
    row = connection.execute(
        "SELECT * FROM jobs WHERE id = ?", (claim.id,)
    ).fetchone()
    return BindResult(
        BindDisposition.READY,
        claimed_job_from_row(row),
        superseded_job_ids=superseded_ids,
    )


def _supersede_pending_successors(
    connection: sqlite3.Connection,
    claim: ClaimedJob,
    profile_version: str,
    observed_at_text: str,
    now_text: str,
) -> tuple[int, ...]:
    if claim.force:
        return ()
    rows = connection.execute(
        """
        SELECT id FROM jobs
        WHERE document_id = ? AND id > ? AND profile_version = ?
          AND last_delivery_at <= ? AND force = 0
          AND content_hash IS NULL AND state IN ('pending', 'retry_wait')
        ORDER BY id
        """,
        (
            claim.document_id,
            claim.id,
            profile_version,
            observed_at_text,
        ),
    ).fetchall()
    if rows:
        connection.executemany(
            """
            UPDATE jobs SET state = 'superseded', updated_at = ?,
                completed_at = ? WHERE id = ?
            """,
            [(now_text, now_text, row["id"]) for row in rows],
        )
    return tuple(row["id"] for row in rows)


def complete(
    database: Database,
    clock: Callable[[], datetime],
    claim: ClaimedJob,
    *,
    model_name: str,
    input_tokens: int,
    output_tokens: int,
    latency_ms: int,
) -> Job:
    _validate_completion_metrics(model_name, input_tokens, output_tokens, latency_ms)
    with database.transaction(immediate=True) as connection:
        now = require_utc(clock())
        durable = leased_job(connection, claim, to_text(now))
        if durable.content_hash is None:
            raise ValueError("complete requires a bound identity")
        state = _completion_state(connection, durable)
        update = TerminalUpdate(state=state, at=now)
        if state is JobState.COMPLETED:
            update = TerminalUpdate(
                state=state,
                at=now,
                model_name=model_name,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=latency_ms,
            )
        return _terminal_in_transaction(connection, durable, update)


def _validate_completion_metrics(
    model_name: str,
    input_tokens: int,
    output_tokens: int,
    latency_ms: int,
) -> None:
    if not isinstance(model_name, str) or not model_name:
        raise ValueError("model_name must be non-empty")
    for name, value in (
        ("input_tokens", input_tokens),
        ("output_tokens", output_tokens),
        ("latency_ms", latency_ms),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")


def _completion_state(
    connection: sqlite3.Connection, claim: ClaimedJob
) -> JobState:
    if claim.force:
        return JobState.COMPLETED
    duplicate = connection.execute(
        """
        SELECT id FROM jobs
        WHERE id != ? AND document_id = ? AND content_hash = ?
          AND profile_version = ? AND force = 0
          AND state = 'completed'
        LIMIT 1
        """,
        (
            claim.id,
            claim.document_id,
            claim.content_hash,
            claim.profile_version,
        ),
    ).fetchone()
    return JobState.SUPERSEDED if duplicate is not None else JobState.COMPLETED


def retry(
    database: Database,
    config: WorkerConfig,
    clock: Callable[[], datetime],
    uniform: Callable[[float, float], float],
    claim: ClaimedJob,
    error: JobError,
) -> Job:
    with database.transaction(immediate=True) as connection:
        now = require_utc(clock())
        now_text = to_text(now)
        durable = leased_job(connection, claim, now_text)
        if durable.attempt_count >= config.max_attempts:
            return _terminal_in_transaction(
                connection,
                durable,
                TerminalUpdate(JobState.FAILED, now, error=error),
            )
        return _schedule_retry(
            connection, config, uniform, durable, error, now, now_text
        )


def _schedule_retry(
    connection: sqlite3.Connection,
    config: WorkerConfig,
    uniform: Callable[[float, float], float],
    claim: ClaimedJob,
    error: JobError,
    now: datetime,
    now_text: str,
) -> Job:
    error_class, error_message, record = safe_error(error, now)
    history = (*claim.error_history, record)
    exponential = config.retry_base_seconds * 2 ** (claim.attempt_count - 1)
    jitter = uniform(0, config.retry_jitter_seconds)
    if not 0 <= jitter <= config.retry_jitter_seconds:
        raise ValueError("uniform source returned jitter outside requested range")
    delay = min(config.retry_max_seconds, exponential + jitter)
    next_attempt = now + timedelta(seconds=delay)
    cursor = connection.execute(
        """
        UPDATE jobs
        SET state = 'retry_wait', updated_at = ?, next_attempt_at = ?,
            completed_at = NULL, lease_owner = NULL, lease_token = NULL,
            lease_expires_at = NULL, last_error_class = ?,
            last_error_message = ?, error_history = ?
        WHERE id = ? AND state = 'leased' AND lease_token = ?
          AND lease_expires_at > ?
        """,
        (
            now_text,
            to_text(next_attempt),
            error_class,
            error_message,
            json.dumps(history),
            claim.id,
            claim.lease_token,
            now_text,
        ),
    )
    require_changed(cursor)
    row = connection.execute(
        "SELECT * FROM jobs WHERE id = ?", (claim.id,)
    ).fetchone()
    return job_from_row(row)


def terminal(
    database: Database,
    clock: Callable[[], datetime],
    claim: ClaimedJob,
    state: JobState,
    *,
    error: JobError | None = None,
) -> Job:
    with database.transaction(immediate=True) as connection:
        now = require_utc(clock())
        durable = leased_job(connection, claim, to_text(now))
        return _terminal_in_transaction(
            connection, durable, TerminalUpdate(state, now, error=error)
        )


def _terminal_in_transaction(
    connection: sqlite3.Connection,
    claim: ClaimedJob,
    update: TerminalUpdate,
) -> Job:
    error_class, error_message, history = _terminal_error(claim, update)
    now_text = to_text(update.at)
    cursor = connection.execute(
        """
        UPDATE jobs
        SET state = ?, updated_at = ?, completed_at = ?,
            lease_owner = NULL, lease_token = NULL,
            lease_expires_at = NULL, last_error_class = ?,
            last_error_message = ?, error_history = ?, model_name = ?,
            input_tokens = ?, output_tokens = ?, latency_ms = ?
        WHERE id = ? AND state = 'leased' AND lease_token = ?
          AND lease_expires_at > ?
        """,
        (
            update.state.value,
            now_text,
            now_text,
            error_class,
            error_message,
            json.dumps(history),
            update.model_name,
            update.input_tokens,
            update.output_tokens,
            update.latency_ms,
            claim.id,
            claim.lease_token,
            now_text,
        ),
    )
    require_changed(cursor)
    row = connection.execute(
        "SELECT * FROM jobs WHERE id = ?", (claim.id,)
    ).fetchone()
    return job_from_row(row)


def _terminal_error(
    claim: ClaimedJob,
    update: TerminalUpdate,
) -> tuple[str | None, str | None, tuple[dict[str, object], ...]]:
    if update.error is None:
        return None, None, claim.error_history
    error_class, error_message, record = safe_error(update.error, update.at)
    return error_class, error_message, (*claim.error_history, record)


def renew_lease(
    database: Database,
    config: WorkerConfig,
    clock: Callable[[], datetime],
    claim: ClaimedJob,
) -> ClaimedJob:
    with database.transaction(immediate=True) as connection:
        now = require_utc(clock())
        expires = now + timedelta(seconds=config.lease_seconds)
        cursor = connection.execute(
            """
            UPDATE jobs SET lease_expires_at = ?, updated_at = ?
            WHERE id = ? AND state = 'leased' AND lease_token = ?
              AND lease_expires_at > ?
            """,
            (
                to_text(expires),
                to_text(now),
                claim.id,
                claim.lease_token,
                to_text(now),
            ),
        )
        require_changed(cursor)
        row = connection.execute(
            "SELECT * FROM jobs WHERE id = ?", (claim.id,)
        ).fetchone()
        return claimed_job_from_row(row)


def manual_retry(
    database: Database,
    clock: Callable[[], datetime],
    job_id: int,
) -> Job:
    if isinstance(job_id, bool) or not isinstance(job_id, int) or job_id <= 0:
        raise ValueError("job_id must be a positive integer")
    now = require_utc(clock())
    with database.transaction(immediate=True) as connection:
        row = connection.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if row is None or row["state"] != JobState.FAILED.value:
            raise ValueError("manual retry requires a failed job")
        load_error_history(row["error_history"])
        _ensure_no_pending_successor(connection, row)
        connection.execute(
            """
            UPDATE jobs
            SET state = 'pending', attempt_count = 0,
                retry_epoch = retry_epoch + 1, next_attempt_at = NULL,
                updated_at = ?, completed_at = NULL, lease_owner = NULL,
                lease_token = NULL, lease_expires_at = NULL
            WHERE id = ? AND state = 'failed'
            """,
            (to_text(now), job_id),
        )
        retried = connection.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return job_from_row(retried)


def _ensure_no_pending_successor(
    connection: sqlite3.Connection, row: sqlite3.Row
) -> None:
    if row["force"]:
        return
    successor = connection.execute(
        """
        SELECT id FROM jobs
        WHERE id > ? AND document_id = ? AND force = 0
          AND state IN ('pending', 'retry_wait')
        LIMIT 1
        """,
        (row["id"], row["document_id"]),
    ).fetchone()
    if successor is not None:
        raise ManualRetryConflictError(
            "newer unresolved normal work blocks manual retry"
        )
