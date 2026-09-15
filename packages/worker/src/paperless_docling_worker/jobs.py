from __future__ import annotations

import json
import random
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Callable
from uuid import UUID, uuid4

from .config import WorkerConfig
from .db import Database

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")


class JobState(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    RETRY_WAIT = "retry_wait"
    COMPLETED = "completed"
    SUPERSEDED = "superseded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class BindDisposition(StrEnum):
    READY = "ready"
    ALREADY_COMPLETED = "already_completed"


class LostLeaseError(RuntimeError):
    """Raised when a worker no longer owns a live job lease."""


class EnqueueConflictError(RuntimeError):
    """Raised when a forced request UUID is reused for another payload."""


class ManualRetryConflictError(RuntimeError):
    """Raised when newer unresolved normal work blocks a manual retry."""


class JobInvariantError(RuntimeError):
    """Raised when persisted job state violates repository invariants."""


@dataclass(frozen=True)
class JobError:
    error_class: str
    message: str
    code: str | None = None


@dataclass(frozen=True)
class Job:
    id: int
    document_id: int
    event: str
    state: JobState
    force: bool
    request_id: UUID | None
    content_hash: str | None
    profile_version: str
    attempt_count: int
    cumulative_attempts: int
    retry_epoch: int
    next_attempt_at: datetime | None
    lease_owner: str | None
    lease_token: str | None
    lease_expires_at: datetime | None
    last_delivery_at: datetime
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    last_error_class: str | None
    last_error_message: str | None
    error_history: tuple[dict[str, object], ...]
    model_name: str | None
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int | None


@dataclass(frozen=True)
class ClaimedJob(Job):
    lease_owner: str
    lease_token: str
    lease_expires_at: datetime


@dataclass(frozen=True)
class BindResult:
    disposition: BindDisposition
    job: Job
    duplicate_of: int | None = None
    superseded_job_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class JobMetrics:
    state_totals: dict[JobState, int]
    ready_queue_depth: int
    oldest_eligible_age_seconds: float


class JobRepository:
    def __init__(
        self,
        database: Database,
        config: WorkerConfig,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        uniform: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self._database = database
        self._config = config
        self._clock = clock
        self._uniform = uniform

    def enqueue(
        self,
        document_id: int,
        event: str,
        *,
        force: bool = False,
        request_id: UUID | None = None,
    ) -> Job:
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

        now = _require_utc(self._clock())
        request_text = str(request_id) if request_id is not None else None
        with self._database.transaction(immediate=True) as connection:
            if request_text is not None:
                existing = connection.execute(
                    "SELECT * FROM jobs WHERE request_id = ?", (request_text,)
                ).fetchone()
                if existing is not None:
                    if (
                        existing["document_id"] != document_id
                        or existing["event"] != event
                    ):
                        raise EnqueueConflictError(
                            "request_id is already bound to another payload"
                        )
                    return _job_from_row(existing)

            if not force:
                existing = connection.execute(
                    """
                    SELECT * FROM jobs
                    WHERE document_id = ? AND profile_version = ?
                      AND force = 0 AND content_hash IS NULL
                      AND state IN ('pending', 'retry_wait')
                    """,
                    (document_id, self._config.profile_version),
                ).fetchone()
                if existing is not None:
                    connection.execute(
                        """
                        UPDATE jobs SET event = ?, state = 'pending',
                            next_attempt_at = NULL, last_delivery_at = ?,
                            updated_at = ? WHERE id = ?
                        """,
                        (event, _to_text(now), _to_text(now), existing["id"]),
                    )
                    row = connection.execute(
                        "SELECT * FROM jobs WHERE id = ?", (existing["id"],)
                    ).fetchone()
                    return _job_from_row(row)

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
                    self._config.profile_version,
                    _to_text(now),
                    _to_text(now),
                    _to_text(now),
                ),
            )
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            return _job_from_row(row)

    def check_ready(self) -> None:
        self._database.check_ready()

    def metrics_snapshot(self) -> JobMetrics:
        now = _require_utc(self._clock())
        now_text = _to_text(now)
        state_totals = {state: 0 for state in JobState}
        with self._database.transaction() as connection:
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
                (now_text, now_text, self._config.max_attempts),
            ).fetchone()
        oldest = _from_text(eligible["oldest"])
        age = max(0.0, (now - oldest).total_seconds()) if oldest else 0.0
        return JobMetrics(state_totals, eligible["depth"], age)

    def claim(self, worker_id: str, now: datetime) -> ClaimedJob | None:
        if not isinstance(worker_id, str) or not worker_id:
            raise ValueError("worker_id must be non-empty")
        now = _require_utc(now)
        now_text = _to_text(now)
        lease_expires_at = now + timedelta(seconds=self._config.lease_seconds)
        token = str(uuid4())

        with self._database.transaction(immediate=True) as connection:
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
                if (
                    row["state"] == JobState.LEASED.value
                    and row["attempt_count"] >= self._config.max_attempts
                ):
                    self._fail_exhausted_lease(connection, row, now)
                    continue
                break

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
                    _to_text(lease_expires_at),
                    now_text,
                    now_text,
                    row["id"],
                ),
            )
            claimed = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (row["id"],)
            ).fetchone()
            return _claimed_job_from_row(claimed)

    @staticmethod
    def _fail_exhausted_lease(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        now: datetime,
    ) -> None:
        error_class, error_message, record = _safe_error(
            JobError(
                "LeaseExpired",
                "lease expired after maximum attempts",
                code="lease-expired",
            ),
            now,
        )
        history = (*_load_error_history(row["error_history"]), record)
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
                _to_text(now),
                _to_text(now),
                error_class,
                error_message,
                json.dumps(history),
                row["id"],
                row["lease_token"],
                _to_text(now),
                row["attempt_count"],
            ),
        )
        _require_changed(cursor)

    def bind_content(
        self,
        claim: ClaimedJob,
        content_hash: str,
        profile_version: str,
        observed_at: datetime,
    ) -> BindResult:
        if not isinstance(content_hash, str) or not content_hash:
            raise ValueError("content_hash must be non-empty")
        if not isinstance(profile_version, str) or not profile_version:
            raise ValueError("profile_version must be non-empty")
        observed_at = _require_utc(observed_at)

        with self._database.transaction(immediate=True) as connection:
            now_text = _to_text(self._clock())
            durable = _leased_job(connection, claim, now_text)
            duplicate = None
            if not durable.force:
                duplicate = connection.execute(
                    """
                    SELECT id FROM jobs
                    WHERE document_id = ? AND content_hash = ?
                      AND profile_version = ? AND force = 0
                      AND state = 'completed'
                    ORDER BY id LIMIT 1
                    """,
                    (durable.document_id, content_hash, profile_version),
                ).fetchone()

            if duplicate is not None:
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
                        durable.id,
                        durable.lease_token,
                        now_text,
                    ),
                )
                _require_changed(cursor)
                superseded_ids = self._supersede_pending_successors(
                    connection,
                    durable,
                    profile_version,
                    _to_text(observed_at),
                    now_text,
                )
                row = connection.execute(
                    "SELECT * FROM jobs WHERE id = ?", (durable.id,)
                ).fetchone()
                return BindResult(
                    BindDisposition.ALREADY_COMPLETED,
                    _job_from_row(row),
                    duplicate_of=duplicate["id"],
                    superseded_job_ids=superseded_ids,
                )

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
                    durable.id,
                    durable.lease_token,
                    now_text,
                ),
            )
            _require_changed(cursor)
            superseded_ids = self._supersede_pending_successors(
                connection,
                durable,
                profile_version,
                _to_text(observed_at),
                now_text,
            )
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (durable.id,)
            ).fetchone()
            return BindResult(
                BindDisposition.READY,
                _claimed_job_from_row(row),
                superseded_job_ids=superseded_ids,
            )

    def complete(
        self,
        claim: ClaimedJob,
        *,
        model_name: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: int,
    ) -> Job:
        if not isinstance(model_name, str) or not model_name:
            raise ValueError("model_name must be non-empty")
        for name, value in (
            ("input_tokens", input_tokens),
            ("output_tokens", output_tokens),
            ("latency_ms", latency_ms),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        with self._database.transaction(immediate=True) as connection:
            now = _require_utc(self._clock())
            now_text = _to_text(now)
            durable = _leased_job(connection, claim, now_text)
            if durable.content_hash is None:
                raise ValueError("complete requires a bound identity")
            state = JobState.COMPLETED
            if not durable.force and durable.content_hash is not None:
                duplicate = connection.execute(
                    """
                    SELECT id FROM jobs
                    WHERE id != ? AND document_id = ? AND content_hash = ?
                      AND profile_version = ? AND force = 0
                      AND state = 'completed'
                    LIMIT 1
                    """,
                    (
                        durable.id,
                        durable.document_id,
                        durable.content_hash,
                        durable.profile_version,
                    ),
                ).fetchone()
                if duplicate is not None:
                    state = JobState.SUPERSEDED
            return self._terminal_in_transaction(
                connection,
                durable,
                state,
                now,
                model_name=model_name if state is JobState.COMPLETED else None,
                input_tokens=input_tokens if state is JobState.COMPLETED else None,
                output_tokens=output_tokens if state is JobState.COMPLETED else None,
                latency_ms=latency_ms if state is JobState.COMPLETED else None,
            )

    def retry(self, claim: ClaimedJob, error: JobError) -> Job:
        with self._database.transaction(immediate=True) as connection:
            now = _require_utc(self._clock())
            now_text = _to_text(now)
            durable = _leased_job(connection, claim, now_text)
            if durable.attempt_count >= self._config.max_attempts:
                return self._terminal_in_transaction(
                    connection, durable, JobState.FAILED, now, error=error
                )

            error_class, error_message, record = _safe_error(error, now)
            history = (*durable.error_history, record)
            exponential = self._config.retry_base_seconds * 2 ** (
                durable.attempt_count - 1
            )
            jitter = self._uniform(0, self._config.retry_jitter_seconds)
            if not 0 <= jitter <= self._config.retry_jitter_seconds:
                raise ValueError(
                    "uniform source returned jitter outside requested range"
                )
            delay = min(self._config.retry_max_seconds, exponential + jitter)
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
                    _to_text(next_attempt),
                    error_class,
                    error_message,
                    json.dumps(history),
                    durable.id,
                    durable.lease_token,
                    now_text,
                ),
            )
            _require_changed(cursor)
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (durable.id,)
            ).fetchone()
            return _job_from_row(row)

    def fail(self, claim: ClaimedJob, error: JobError) -> Job:
        return self._terminal(claim, JobState.FAILED, error=error)

    def cancel(self, claim: ClaimedJob) -> Job:
        return self._terminal(claim, JobState.CANCELLED)

    def supersede(self, claim: ClaimedJob) -> Job:
        return self._terminal(claim, JobState.SUPERSEDED)

    def renew_lease(self, claim: ClaimedJob) -> ClaimedJob:
        with self._database.transaction(immediate=True) as connection:
            now = _require_utc(self._clock())
            expires = now + timedelta(seconds=self._config.lease_seconds)
            cursor = connection.execute(
                """
                UPDATE jobs SET lease_expires_at = ?, updated_at = ?
                WHERE id = ? AND state = 'leased' AND lease_token = ?
                  AND lease_expires_at > ?
                """,
                (
                    _to_text(expires),
                    _to_text(now),
                    claim.id,
                    claim.lease_token,
                    _to_text(now),
                ),
            )
            _require_changed(cursor)
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (claim.id,)
            ).fetchone()
            return _claimed_job_from_row(row)

    def manual_retry(self, job_id: int) -> Job:
        if isinstance(job_id, bool) or not isinstance(job_id, int) or job_id <= 0:
            raise ValueError("job_id must be a positive integer")
        now = _require_utc(self._clock())
        with self._database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None or row["state"] != JobState.FAILED.value:
                raise ValueError("manual retry requires a failed job")
            _load_error_history(row["error_history"])
            if not row["force"]:
                successor = connection.execute(
                    """
                    SELECT id FROM jobs
                    WHERE id > ? AND document_id = ? AND force = 0
                      AND state IN ('pending', 'retry_wait')
                    LIMIT 1
                    """,
                    (job_id, row["document_id"]),
                ).fetchone()
                if successor is not None:
                    raise ManualRetryConflictError(
                        "newer unresolved normal work blocks manual retry"
                    )
            connection.execute(
                """
                UPDATE jobs
                SET state = 'pending', attempt_count = 0,
                    retry_epoch = retry_epoch + 1, next_attempt_at = NULL,
                    updated_at = ?, completed_at = NULL, lease_owner = NULL,
                    lease_token = NULL, lease_expires_at = NULL
                WHERE id = ? AND state = 'failed'
                """,
                (_to_text(now), job_id),
            )
            retried = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            return _job_from_row(retried)

    def _terminal(
        self,
        claim: ClaimedJob,
        state: JobState,
        *,
        error: JobError | None = None,
        model_name: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        latency_ms: int | None = None,
    ) -> Job:
        with self._database.transaction(immediate=True) as connection:
            now = _require_utc(self._clock())
            now_text = _to_text(now)
            durable = _leased_job(connection, claim, now_text)
            return self._terminal_in_transaction(
                connection,
                durable,
                state,
                now,
                error=error,
                model_name=model_name,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=latency_ms,
            )

    @staticmethod
    def _terminal_in_transaction(
        connection: sqlite3.Connection,
        claim: ClaimedJob,
        state: JobState,
        now: datetime,
        *,
        error: JobError | None = None,
        model_name: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        latency_ms: int | None = None,
    ) -> Job:
        error_class = None
        error_message = None
        history = claim.error_history
        if error is not None:
            error_class, error_message, record = _safe_error(error, now)
            history = (*history, record)
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
                state.value,
                _to_text(now),
                _to_text(now),
                error_class,
                error_message,
                json.dumps(history),
                model_name,
                input_tokens,
                output_tokens,
                latency_ms,
                claim.id,
                claim.lease_token,
                _to_text(now),
            ),
        )
        _require_changed(cursor)
        row = connection.execute(
            "SELECT * FROM jobs WHERE id = ?", (claim.id,)
        ).fetchone()
        return _job_from_row(row)

    @staticmethod
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


def _require_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ValueError("datetime must be in UTC")
    return value.astimezone(UTC)


def _require_changed(cursor: sqlite3.Cursor) -> None:
    if cursor.rowcount != 1:
        raise LostLeaseError("job lease is no longer owned")


def _leased_job(
    connection: sqlite3.Connection,
    claim: ClaimedJob,
    now_text: str,
) -> ClaimedJob:
    row = connection.execute(
        """
        SELECT * FROM jobs
        WHERE id = ? AND state = 'leased' AND lease_token = ?
          AND lease_expires_at > ?
        """,
        (claim.id, claim.lease_token, now_text),
    ).fetchone()
    if row is None:
        raise LostLeaseError("job lease is no longer owned")
    return _claimed_job_from_row(row)


def _safe_error(
    error: JobError, now: datetime
) -> tuple[str, str, dict[str, object]]:
    if not isinstance(error.error_class, str) or not _SAFE_IDENTIFIER.fullmatch(
        error.error_class
    ):
        raise ValueError("error_class must be a safe identifier")
    if error.code is not None and (
        not isinstance(error.code, str) or not _SAFE_IDENTIFIER.fullmatch(error.code)
    ):
        raise ValueError("error code must be a safe identifier")
    if not isinstance(error.message, str):
        raise ValueError("error message must be text")
    message = "".join(
        character for character in error.message if character.isprintable()
    )[:500]
    record: dict[str, object] = {
        "at": _to_text(now),
        "class": error.error_class,
        "message": message,
    }
    if error.code is not None:
        record["code"] = error.code
    return error.error_class, message, record


def _to_text(value: datetime) -> str:
    return _require_utc(value).isoformat()


def _from_text(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def _load_error_history(value: str) -> tuple[dict[str, object], ...]:
    try:
        history = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        raise ValueError("stored error history is not valid JSON") from None
    if not isinstance(history, list) or not all(
        isinstance(record, dict) for record in history
    ):
        raise ValueError("stored error history must be a JSON array of records")
    for record in history:
        if set(record) - {"at", "class", "message", "code"}:
            raise ValueError("stored error history contains unexpected fields")
        if (
            not isinstance(record.get("class"), str)
            or not _SAFE_IDENTIFIER.fullmatch(record["class"])
            or not isinstance(record.get("message"), str)
            or len(record["message"]) > 500
            or not record["message"].isprintable()
        ):
            raise ValueError("stored error history contains an invalid record")
        code = record.get("code")
        if code is not None and (
            not isinstance(code, str) or not _SAFE_IDENTIFIER.fullmatch(code)
        ):
            raise ValueError("stored error history contains an invalid code")
        try:
            recorded_at = datetime.fromisoformat(record["at"])
            _require_utc(recorded_at)
        except (KeyError, TypeError, ValueError):
            raise ValueError("stored error history contains an invalid time") from None
    return tuple(history)


def _job_from_row(row: sqlite3.Row) -> Job:
    request_id = UUID(row["request_id"]) if row["request_id"] else None
    return Job(
        id=row["id"],
        document_id=row["document_id"],
        event=row["event"],
        state=JobState(row["state"]),
        force=bool(row["force"]),
        request_id=request_id,
        content_hash=row["content_hash"],
        profile_version=row["profile_version"],
        attempt_count=row["attempt_count"],
        cumulative_attempts=row["cumulative_attempts"],
        retry_epoch=row["retry_epoch"],
        next_attempt_at=_from_text(row["next_attempt_at"]),
        lease_owner=row["lease_owner"],
        lease_token=row["lease_token"],
        lease_expires_at=_from_text(row["lease_expires_at"]),
        last_delivery_at=_from_text(row["last_delivery_at"]),  # type: ignore[arg-type]
        created_at=_from_text(row["created_at"]),  # type: ignore[arg-type]
        updated_at=_from_text(row["updated_at"]),  # type: ignore[arg-type]
        started_at=_from_text(row["started_at"]),
        completed_at=_from_text(row["completed_at"]),
        last_error_class=row["last_error_class"],
        last_error_message=row["last_error_message"],
        error_history=_load_error_history(row["error_history"]),
        model_name=row["model_name"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        latency_ms=row["latency_ms"],
    )


def _claimed_job_from_row(row: sqlite3.Row) -> ClaimedJob:
    job = _job_from_row(row)
    if job.state is not JobState.LEASED:
        raise JobInvariantError("claimed job state must be leased")
    if job.lease_owner is None:
        raise JobInvariantError("claimed job lease_owner must be present")
    if job.lease_token is None:
        raise JobInvariantError("claimed job lease_token must be present")
    if job.lease_expires_at is None:
        raise JobInvariantError("claimed job lease_expires_at must be present")
    return ClaimedJob(**job.__dict__)
