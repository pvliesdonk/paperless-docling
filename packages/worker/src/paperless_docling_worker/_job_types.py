from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


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
