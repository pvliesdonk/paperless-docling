from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import UUID

from ._job_types import (
    ClaimedJob,
    Job,
    JobError,
    JobInvariantError,
    JobState,
    LostLeaseError,
)

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_ERROR_FIELDS = {"at", "class", "message", "code"}


def require_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ValueError("datetime must be in UTC")
    return value.astimezone(UTC)


def to_text(value: datetime) -> str:
    return require_utc(value).isoformat()


def from_text(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def require_changed(cursor: sqlite3.Cursor) -> None:
    if cursor.rowcount != 1:
        raise LostLeaseError("job lease is no longer owned")


def leased_job(
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
    return claimed_job_from_row(row)


def safe_error(
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
        "at": to_text(now),
        "class": error.error_class,
        "message": message,
    }
    if error.code is not None:
        record["code"] = error.code
    return error.error_class, message, record


def load_error_history(value: str) -> tuple[dict[str, object], ...]:
    history = _decode_error_history(value)
    for record in history:
        _validate_error_record(record)
    return tuple(history)


def _decode_error_history(value: str) -> list[dict[str, object]]:
    try:
        history = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        raise ValueError("stored error history is not valid JSON") from None
    if not isinstance(history, list) or not all(
        isinstance(record, dict) for record in history
    ):
        raise ValueError("stored error history must be a JSON array of records")
    return history


def _validate_error_record(record: dict[str, object]) -> None:
    if set(record) - _ERROR_FIELDS:
        raise ValueError("stored error history contains unexpected fields")
    if not _valid_error_record_fields(record):
        raise ValueError("stored error history contains an invalid record")
    code = record.get("code")
    if code is not None and (
        not isinstance(code, str) or not _SAFE_IDENTIFIER.fullmatch(code)
    ):
        raise ValueError("stored error history contains an invalid code")
    _validate_recorded_at(record)


def _valid_error_record_fields(record: dict[str, object]) -> bool:
    error_class = record.get("class")
    message = record.get("message")
    return (
        isinstance(error_class, str)
        and _SAFE_IDENTIFIER.fullmatch(error_class) is not None
        and isinstance(message, str)
        and len(message) <= 500
        and message.isprintable()
    )


def _validate_recorded_at(record: dict[str, object]) -> None:
    try:
        recorded_at = datetime.fromisoformat(record["at"])  # type: ignore[arg-type]
        require_utc(recorded_at)
    except (KeyError, TypeError, ValueError):
        raise ValueError("stored error history contains an invalid time") from None


def job_from_row(row: sqlite3.Row) -> Job:
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
        next_attempt_at=from_text(row["next_attempt_at"]),
        lease_owner=row["lease_owner"],
        lease_token=row["lease_token"],
        lease_expires_at=from_text(row["lease_expires_at"]),
        last_delivery_at=from_text(row["last_delivery_at"]),  # type: ignore[arg-type]
        created_at=from_text(row["created_at"]),  # type: ignore[arg-type]
        updated_at=from_text(row["updated_at"]),  # type: ignore[arg-type]
        started_at=from_text(row["started_at"]),
        completed_at=from_text(row["completed_at"]),
        last_error_class=row["last_error_class"],
        last_error_message=row["last_error_message"],
        error_history=load_error_history(row["error_history"]),
        model_name=row["model_name"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        latency_ms=row["latency_ms"],
    )


def claimed_job_from_row(row: sqlite3.Row) -> ClaimedJob:
    job = job_from_row(row)
    if job.state is not JobState.LEASED:
        raise JobInvariantError("claimed job state must be leased")
    if job.lease_owner is None:
        raise JobInvariantError("claimed job lease_owner must be present")
    if job.lease_token is None:
        raise JobInvariantError("claimed job lease_token must be present")
    if job.lease_expires_at is None:
        raise JobInvariantError("claimed job lease_expires_at must be present")
    return ClaimedJob(**job.__dict__)
