from __future__ import annotations

import random
from datetime import UTC, datetime
from typing import Callable
from uuid import UUID

from ._job_queue import claim as _claim
from ._job_queue import enqueue as _enqueue
from ._job_queue import metrics_snapshot as _metrics_snapshot
from ._job_transitions import bind_content as _bind_content
from ._job_transitions import complete as _complete
from ._job_transitions import manual_retry as _manual_retry
from ._job_transitions import renew_lease as _renew_lease
from ._job_transitions import retry as _retry
from ._job_transitions import terminal as _terminal
from ._job_types import (
    BindDisposition,
    BindResult,
    ClaimedJob,
    EnqueueConflictError,
    Job,
    JobError,
    JobInvariantError,
    JobMetrics,
    JobState,
    LostLeaseError,
    ManualRetryConflictError,
)
from .config import WorkerConfig
from .db import Database

__all__ = [
    "BindDisposition",
    "BindResult",
    "ClaimedJob",
    "EnqueueConflictError",
    "Job",
    "JobError",
    "JobInvariantError",
    "JobMetrics",
    "JobRepository",
    "JobState",
    "LostLeaseError",
    "ManualRetryConflictError",
]


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
        return _enqueue(
            self._database,
            self._config,
            self._clock,
            document_id,
            event,
            force=force,
            request_id=request_id,
        )

    def check_ready(self) -> None:
        self._database.check_ready()

    def metrics_snapshot(self) -> JobMetrics:
        return _metrics_snapshot(self._database, self._config, self._clock)

    def claim(self, worker_id: str, now: datetime) -> ClaimedJob | None:
        return _claim(self._database, self._config, worker_id, now)

    def bind_content(
        self,
        claim: ClaimedJob,
        content_hash: str,
        profile_version: str,
        observed_at: datetime,
    ) -> BindResult:
        return _bind_content(
            self._database,
            self._clock,
            claim,
            content_hash,
            profile_version,
            observed_at,
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
        return _complete(
            self._database,
            self._clock,
            claim,
            model_name=model_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
        )

    def retry(self, claim: ClaimedJob, error: JobError) -> Job:
        return _retry(
            self._database,
            self._config,
            self._clock,
            self._uniform,
            claim,
            error,
        )

    def fail(self, claim: ClaimedJob, error: JobError) -> Job:
        return _terminal(
            self._database, self._clock, claim, JobState.FAILED, error=error
        )

    def cancel(self, claim: ClaimedJob) -> Job:
        return _terminal(self._database, self._clock, claim, JobState.CANCELLED)

    def supersede(self, claim: ClaimedJob) -> Job:
        return _terminal(self._database, self._clock, claim, JobState.SUPERSEDED)

    def renew_lease(self, claim: ClaimedJob) -> ClaimedJob:
        return _renew_lease(self._database, self._config, self._clock, claim)

    def manual_retry(self, job_id: int) -> Job:
        return _manual_retry(self._database, self._clock, job_id)
