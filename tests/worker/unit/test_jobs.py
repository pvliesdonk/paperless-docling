from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from paperless_docling_worker.config import WorkerConfig
from paperless_docling_worker.db import Database
from paperless_docling_worker.jobs import (
    BindDisposition,
    EnqueueConflictError,
    JobError,
    JobInvariantError,
    JobRepository,
    JobState,
    LostLeaseError,
    ManualRetryConflictError,
)

NOW = datetime(2026, 8, 27, 12, tzinfo=UTC)


def worker_config(path: Path, **overrides: object) -> WorkerConfig:
    config = WorkerConfig(
        paperless_url="https://paperless.example.test",
        paperless_token_file=path / "paperless-token",
        webhook_token_file=path / "webhook-token",
        database_path=path / "summary.sqlite3",
        llm_base_url="https://llm.example.test/v1",
        llm_api_key_file=path / "llm-token",
        llm_model="summary-model",
        profile_version="profile-v1",
        context_tokens=32_768,
        output_tokens=1_024,
        chunk_tokens=12_000,
        max_attempts=5,
        lease_seconds=300,
        retry_base_seconds=5,
        retry_max_seconds=300,
        retry_jitter_seconds=1,
        max_request_bytes=65_536,
        concurrency=1,
        paperless_token="paperless-secret",  # type: ignore[arg-type]
        webhook_token="webhook-secret",  # type: ignore[arg-type]
        llm_api_key="llm-secret",  # type: ignore[arg-type]
    )
    return replace(config, **overrides)


@pytest.fixture
def database(tmp_path):
    database = Database.open(tmp_path / "summary.sqlite3")
    database.migrate()
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def repository(database, tmp_path):
    return JobRepository(database, worker_config(tmp_path), clock=lambda: NOW)


def test_normal_duplicate_events_coalesce_before_content_resolution(repository):
    first = repository.enqueue(41, "document_added")
    duplicate = repository.enqueue(41, "document_updated")

    assert duplicate.id == first.id
    assert duplicate.event == "document_updated"
    assert duplicate.state is JobState.PENDING
    assert first.last_delivery_at == NOW
    assert duplicate.last_delivery_at == NOW


def test_metrics_snapshot_cannot_mix_state_before_mutation_with_queue_after_it(
    tmp_path,
):
    path = tmp_path / "summary.sqlite3"
    setup = Database.open(path)
    setup.migrate()
    setup.close()
    writer_database = Database.open(path)
    writer_repository = JobRepository(
        writer_database, worker_config(tmp_path), clock=lambda: NOW
    )
    writer_repository.enqueue(1, "document_added")

    class MutatingConnection(sqlite3.Connection):
        mutated = False

        def execute(self, sql, parameters=(), /):
            cursor = super().execute(sql, parameters)
            if "GROUP BY state" in sql and not self.mutated:
                self.mutated = True
                writer_repository.enqueue(2, "document_added")
            return cursor

    observer_connection = sqlite3.connect(
        path,
        isolation_level=None,
        factory=MutatingConnection,
    )
    observer_connection.row_factory = sqlite3.Row
    observer_connection.execute("PRAGMA journal_mode = WAL")
    observer_database = Database(observer_connection)
    observer_repository = JobRepository(
        observer_database, worker_config(tmp_path), clock=lambda: NOW
    )
    try:
        snapshot = observer_repository.metrics_snapshot()
        assert snapshot.state_totals[JobState.PENDING] == 1
        assert snapshot.ready_queue_depth == 1
        assert writer_repository.metrics_snapshot().ready_queue_depth == 2
    finally:
        observer_database.close()
        writer_database.close()


def test_delivery_while_prior_job_is_leased_creates_pending_successor(repository):
    first = repository.enqueue(41, "document_added")
    claimed = repository.claim("worker-a", NOW)

    successor = repository.enqueue(41, "document_updated")

    assert claimed is not None
    assert claimed.id == first.id
    assert successor.id != first.id
    assert successor.state is JobState.PENDING


@pytest.mark.parametrize("document_id", [0, -1, True, 1.5, "1"])
def test_enqueue_rejects_non_positive_non_integer_document_ids(repository, document_id):
    with pytest.raises(ValueError, match="document_id"):
        repository.enqueue(document_id, "document_added")


@pytest.mark.parametrize("event", ["", "document_deleted", "document_added "])
def test_enqueue_accepts_only_supported_event_values(repository, event):
    with pytest.raises(ValueError, match="event"):
        repository.enqueue(1, event)


def test_enqueue_enforces_normal_and_forced_request_identity(repository):
    with pytest.raises(ValueError, match="request_id"):
        repository.enqueue(1, "document_added", force=True)
    with pytest.raises(ValueError, match="request_id"):
        repository.enqueue(1, "document_added", request_id=uuid4())


@pytest.mark.parametrize("force", [0, 1, "true"])
def test_enqueue_requires_force_to_be_boolean(repository, force):
    with pytest.raises(ValueError, match="force must be boolean"):
        repository.enqueue(1, "document_added", force=force)


def test_forced_request_id_is_globally_idempotent(repository):
    request_id = uuid4()
    first = repository.enqueue(
        41, "document_added", force=True, request_id=request_id
    )
    duplicate = repository.enqueue(
        41, "document_added", force=True, request_id=request_id
    )

    assert duplicate == first
    assert duplicate.request_id == request_id


@pytest.mark.parametrize(
    ("document_id", "event"),
    [(99, "document_added"), (41, "document_updated")],
)
def test_forced_request_id_reuse_with_different_payload_conflicts_without_mutation(
    repository, document_id, event
):
    request_id = uuid4()
    original = repository.enqueue(
        41, "document_added", force=True, request_id=request_id
    )

    with pytest.raises(EnqueueConflictError):
        repository.enqueue(
            document_id, event, force=True, request_id=request_id
        )

    replay = repository.enqueue(
        41, "document_added", force=True, request_id=request_id
    )
    assert replay == original


def test_claim_selects_oldest_eligible_job_then_lowest_id(repository):
    first = repository.enqueue(1, "document_added")
    second = repository.enqueue(2, "document_added")

    claimed_first = repository.claim("worker-a", NOW)
    claimed_second = repository.claim("worker-a", NOW)

    assert claimed_first is not None
    assert claimed_second is not None
    assert (claimed_first.id, claimed_second.id) == (first.id, second.id)


def test_claim_increments_epoch_and_cumulative_attempts_once(repository):
    repository.enqueue(1, "document_added")

    claimed = repository.claim("worker-a", NOW)

    assert claimed is not None
    assert claimed.state is JobState.LEASED
    assert claimed.attempt_count == 1
    assert claimed.cumulative_attempts == 1
    assert claimed.started_at == NOW
    assert claimed.lease_expires_at == NOW + timedelta(seconds=300)


def test_claim_recovers_only_expired_leases(repository):
    repository.enqueue(1, "document_added")
    first = repository.claim("worker-a", NOW)

    assert first is not None
    assert repository.claim("worker-b", NOW + timedelta(seconds=299)) is None

    recovered = repository.claim("worker-b", NOW + timedelta(seconds=300))
    assert recovered is not None
    assert recovered.id == first.id
    assert recovered.attempt_count == 2
    assert recovered.cumulative_attempts == 2
    assert recovered.lease_token != first.lease_token


def test_expired_lease_at_budget_fails_before_next_job_is_claimed(
    database, tmp_path
):
    repository = JobRepository(
        database, worker_config(tmp_path, max_attempts=1), clock=lambda: NOW
    )
    first = repository.enqueue(1, "document_added")
    first_claim = repository.claim("worker-a", NOW)
    assert first_claim is not None
    second = repository.enqueue(2, "document_added")

    next_claim = repository.claim("worker-b", NOW + timedelta(seconds=300))

    assert next_claim is not None
    assert next_claim.id == second.id
    manually_retried = repository.manual_retry(first.id)
    assert manually_retried.cumulative_attempts == 1
    assert manually_retried.last_error_class == "LeaseExpired"


def test_repeated_lease_expiration_never_exceeds_epoch_budget(database, tmp_path):
    repository = JobRepository(
        database, worker_config(tmp_path, max_attempts=2), clock=lambda: NOW
    )
    job = repository.enqueue(1, "document_added")
    first = repository.claim("worker-a", NOW)
    assert first is not None
    second = repository.claim("worker-b", NOW + timedelta(seconds=300))
    assert second is not None
    assert second.attempt_count == 2

    assert repository.claim("worker-c", NOW + timedelta(seconds=600)) is None
    manually_retried = repository.manual_retry(job.id)
    assert manually_retried.attempt_count == 0
    assert manually_retried.cumulative_attempts == 2
    assert manually_retried.retry_epoch == 1


def test_claim_rejects_naive_time(repository):
    repository.enqueue(1, "document_added")

    with pytest.raises(ValueError, match="timezone-aware"):
        repository.claim("worker-a", datetime(2026, 8, 27, 12))


def test_claim_rejects_aware_non_utc_time(repository):
    repository.enqueue(1, "document_added")

    with pytest.raises(ValueError, match="UTC"):
        repository.claim(
            "worker-a", datetime(2026, 8, 27, 13, tzinfo=timezone(timedelta(hours=1)))
        )


def test_claim_uses_unguessable_unique_lease_tokens(repository):
    repository.enqueue(1, "document_added")
    repository.enqueue(2, "document_added")

    first = repository.claim("worker-a", NOW)
    second = repository.claim("worker-b", NOW)

    assert first is not None
    assert second is not None
    assert first.lease_token != second.lease_token
    assert len(first.lease_token) >= 32
    assert UUID(first.lease_token)
    assert UUID(second.lease_token)


def test_claim_is_atomic_across_independent_sqlite_connections(tmp_path):
    path = tmp_path / "summary.sqlite3"
    setup_database = Database.open(path)
    setup_database.migrate()
    setup_database.close()
    barrier = threading.Barrier(2)
    config = worker_config(tmp_path)
    enqueue_database = Database.open(path)
    try:
        job = JobRepository(enqueue_database, config, clock=lambda: NOW).enqueue(
            1, "document_added"
        )
    finally:
        enqueue_database.close()

    class ReservationCheckingConnection(sqlite3.Connection):
        has_write_reservation = False

        def execute(self, sql, parameters=(), /):
            normalized = " ".join(sql.split())
            if normalized == "BEGIN IMMEDIATE":
                cursor = super().execute(sql, parameters)
                self.has_write_reservation = True
                return cursor
            if (
                "OR (state = 'leased' AND lease_expires_at <= ?)" in normalized
                and not self.has_write_reservation
            ):
                raise AssertionError(
                    "claim candidate read occurred without a write reservation"
                )
            return super().execute(sql, parameters)

        def commit(self):
            try:
                return super().commit()
            finally:
                self.has_write_reservation = False

        def rollback(self):
            try:
                return super().rollback()
            finally:
                self.has_write_reservation = False

    def claim(worker_id):
        connection = sqlite3.connect(
            path,
            isolation_level=None,
            timeout=5,
            factory=ReservationCheckingConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        claim_database = Database(connection)
        try:
            repository = JobRepository(claim_database, config, clock=lambda: NOW)
            barrier.wait(timeout=5)
            return repository.claim(worker_id, NOW)
        finally:
            claim_database.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(claim, "worker-a"),
            executor.submit(claim, "worker-b"),
        ]
        claims = [future.result(timeout=10) for future in futures]

    claimed = [claim for claim in claims if claim is not None]
    assert [claim.id for claim in claimed] == [job.id]


def claim_bound(repository, document_id=1, *, content_hash="hash-1"):
    repository.enqueue(document_id, "document_added")
    claim = repository.claim("worker-a", NOW)
    assert claim is not None
    result = repository.bind_content(claim, content_hash, "profile-v1", NOW)
    assert result.disposition is BindDisposition.READY
    return result.job


def test_completed_normal_identity_supersedes_duplicate_before_processing(repository):
    first = claim_bound(repository)
    completed = repository.complete(
        first,
        model_name="summary-model",
        input_tokens=100,
        output_tokens=20,
        latency_ms=250,
    )
    repository.enqueue(1, "document_updated")
    duplicate = repository.claim("worker-b", NOW)
    assert duplicate is not None

    result = repository.bind_content(duplicate, "hash-1", "profile-v1", NOW)

    assert result.disposition is BindDisposition.ALREADY_COMPLETED
    assert result.job.state is JobState.SUPERSEDED
    assert result.duplicate_of == completed.id


def test_forced_job_bypasses_completed_content_identity(repository):
    first = claim_bound(repository)
    repository.complete(
        first,
        model_name="summary-model",
        input_tokens=100,
        output_tokens=20,
        latency_ms=250,
    )
    forced = repository.enqueue(
        1, "document_updated", force=True, request_id=uuid4()
    )
    claim = repository.claim("worker-b", NOW)
    assert claim is not None
    assert claim.id == forced.id

    result = repository.bind_content(claim, "hash-1", "profile-v1", NOW)

    assert result.disposition is BindDisposition.READY
    assert result.job.force is True
    assert result.job.content_hash == "hash-1"


def test_binding_supersedes_successor_delivered_before_content_observation(
    database, tmp_path
):
    clock_time = NOW
    repository = JobRepository(
        database, worker_config(tmp_path), clock=lambda: clock_time
    )
    original = repository.enqueue(1, "document_added")
    claim = repository.claim("worker-a", NOW)
    assert claim is not None
    clock_time = NOW + timedelta(seconds=10)
    successor = repository.enqueue(1, "document_updated")
    assert successor.last_delivery_at == NOW + timedelta(seconds=10)
    observed_at = NOW + timedelta(seconds=20)
    clock_time = NOW + timedelta(seconds=30)

    result = repository.bind_content(
        claim, "hash-current", "profile-v1", observed_at
    )

    assert result.disposition is BindDisposition.READY
    assert result.job.id == original.id
    assert result.superseded_job_ids == (successor.id,)
    assert repository.claim("worker-b", clock_time) is None


def test_binding_preserves_successor_coalesced_after_content_observation(
    database, tmp_path
):
    clock_time = NOW
    repository = JobRepository(
        database, worker_config(tmp_path), clock=lambda: clock_time
    )
    repository.enqueue(1, "document_added")
    predecessor = repository.claim("worker-a", NOW)
    assert predecessor is not None
    clock_time = NOW + timedelta(seconds=10)
    successor = repository.enqueue(1, "document_added")
    observed_at = NOW + timedelta(seconds=20)
    clock_time = NOW + timedelta(seconds=25)

    coalesced = repository.enqueue(1, "document_updated")
    clock_time = NOW + timedelta(seconds=30)
    result = repository.bind_content(
        predecessor, "hash-before-latest-delivery", "profile-v1", observed_at
    )

    assert coalesced.id == successor.id
    assert coalesced.last_delivery_at == NOW + timedelta(seconds=25)
    assert result.superseded_job_ids == ()
    queued = repository.claim("worker-b", clock_time)
    assert queued is not None
    assert queued.id == successor.id


@pytest.mark.parametrize(
    "observed_at",
    [
        datetime(2026, 8, 27, 12),
        datetime(2026, 8, 27, 13, tzinfo=timezone(timedelta(hours=1))),
    ],
)
def test_binding_requires_timezone_aware_utc_observation_without_mutation(
    repository, observed_at
):
    repository.enqueue(1, "document_added")
    claim = repository.claim("worker-a", NOW)
    assert claim is not None

    with pytest.raises(ValueError, match="UTC"):
        repository.bind_content(claim, "hash-1", "profile-v1", observed_at)

    assert repository.renew_lease(claim).id == claim.id


def test_binding_preserves_successor_delivered_after_content_observation(
    database, tmp_path
):
    clock_time = NOW
    repository = JobRepository(
        database, worker_config(tmp_path), clock=lambda: clock_time
    )
    repository.enqueue(1, "document_added")
    claim = repository.claim("worker-a", NOW)
    assert claim is not None
    observed_at = NOW + timedelta(seconds=10)
    clock_time = NOW + timedelta(seconds=20)
    successor = repository.enqueue(1, "document_updated")
    clock_time = NOW + timedelta(seconds=30)

    result = repository.bind_content(claim, "hash-old", "profile-v1", observed_at)

    assert result.superseded_job_ids == ()
    next_claim = repository.claim("worker-b", clock_time)
    assert next_claim is not None
    assert next_claim.id == successor.id


def test_content_observed_before_newer_delivery_is_not_lost(database, tmp_path):
    clock_time = NOW
    repository = JobRepository(
        database, worker_config(tmp_path), clock=lambda: clock_time
    )
    repository.enqueue(1, "document_added")
    first = repository.claim("worker-a", NOW)
    assert first is not None
    observed_at = NOW + timedelta(seconds=5)
    clock_time = NOW + timedelta(seconds=10)
    successor = repository.enqueue(1, "document_updated")
    clock_time = NOW + timedelta(seconds=15)
    first_bound = repository.bind_content(first, "hash-old", "profile-v1", observed_at)
    repository.complete(
        first_bound.job,
        model_name="model",
        input_tokens=1,
        output_tokens=1,
        latency_ms=1,
    )

    next_claim = repository.claim("worker-b", clock_time)

    assert next_claim is not None
    assert next_claim.id == successor.id
    next_bound = repository.bind_content(
        next_claim, "hash-new", "profile-v1", clock_time
    )
    assert next_bound.disposition is BindDisposition.READY


def test_forced_binding_never_supersedes_normal_work(database, tmp_path):
    request_id = uuid4()
    repository = JobRepository(database, worker_config(tmp_path), clock=lambda: NOW)
    forced = repository.enqueue(
        1, "document_added", force=True, request_id=request_id
    )
    forced_claim = repository.claim("worker-a", NOW)
    assert forced_claim is not None
    assert forced_claim.id == forced.id
    normal = repository.enqueue(1, "document_updated")

    result = repository.bind_content(
        forced_claim, "hash-1", "profile-v1", NOW
    )

    assert result.superseded_job_ids == ()
    normal_claim = repository.claim("worker-b", NOW)
    assert normal_claim is not None
    assert normal_claim.id == normal.id


def test_binding_preserves_newer_successor_for_another_profile(database, tmp_path):
    first_repository = JobRepository(
        database, worker_config(tmp_path), clock=lambda: NOW
    )
    first_repository.enqueue(1, "document_added")
    claim = first_repository.claim("worker-a", NOW)
    assert claim is not None
    second_repository = JobRepository(
        database,
        worker_config(tmp_path, profile_version="profile-v2"),
        clock=lambda: NOW,
    )
    successor = second_repository.enqueue(1, "document_updated")

    result = first_repository.bind_content(
        claim, "hash-1", "profile-v1", NOW
    )

    assert result.superseded_job_ids == ()
    next_claim = first_repository.claim("worker-b", NOW)
    assert next_claim is not None
    assert next_claim.id == successor.id


def test_binding_preserves_lower_id_unresolved_job(database, tmp_path):
    repository = JobRepository(
        database,
        worker_config(tmp_path),
        clock=lambda: NOW,
        uniform=lambda start, end: 0,
    )
    older = repository.enqueue(1, "document_added")
    older_claim = repository.claim("worker-a", NOW)
    assert older_claim is not None
    newer = repository.enqueue(1, "document_updated")
    newer_claim = repository.claim("worker-b", NOW)
    assert newer_claim is not None
    repository.retry(older_claim, JobError("temporary", "retry older"))

    result = repository.bind_content(
        newer_claim, "hash-1", "profile-v1", NOW
    )

    assert result.superseded_job_ids == ()
    assert repository.claim("worker-c", NOW + timedelta(seconds=5)).id == older.id
    assert newer_claim.id == newer.id


@pytest.mark.parametrize(
    ("content_hash", "profile_version"),
    [("hash-2", "profile-v1"), ("hash-1", "profile-v2")],
)
def test_changed_content_or_profile_is_a_new_normal_identity(
    repository, content_hash, profile_version
):
    first = claim_bound(repository)
    repository.complete(
        first,
        model_name="summary-model",
        input_tokens=100,
        output_tokens=20,
        latency_ms=250,
    )
    repository.enqueue(1, "document_updated")
    claim = repository.claim("worker-b", NOW)
    assert claim is not None

    result = repository.bind_content(claim, content_hash, profile_version, NOW)

    assert result.disposition is BindDisposition.READY
    assert result.job.content_hash == content_hash
    assert result.job.profile_version == profile_version


def test_summary_update_webhook_successor_deduplicates_after_completion(repository):
    first = claim_bound(repository)
    successor = repository.enqueue(1, "document_updated")
    completed = repository.complete(
        first,
        model_name="summary-model",
        input_tokens=100,
        output_tokens=20,
        latency_ms=250,
    )
    successor_claim = repository.claim("worker-b", NOW)
    assert successor_claim is not None
    assert successor_claim.id == successor.id

    result = repository.bind_content(successor_claim, "hash-1", "profile-v1", NOW)

    assert result.disposition is BindDisposition.ALREADY_COMPLETED
    assert result.duplicate_of == completed.id


def test_terminal_transitions_clear_lease_and_record_completion_metrics(repository):
    claim = claim_bound(repository)

    completed = repository.complete(
        claim,
        model_name="summary-model",
        input_tokens=100,
        output_tokens=20,
        latency_ms=250,
    )

    assert completed.state is JobState.COMPLETED
    assert completed.completed_at == NOW
    assert completed.lease_token is None
    assert completed.model_name == "summary-model"
    assert completed.input_tokens == 100
    assert completed.output_tokens == 20
    assert completed.latency_ms == 250


@pytest.mark.parametrize("force", [False, True])
def test_complete_requires_bound_identity_without_mutating_lease(
    repository, force
):
    request_id = uuid4() if force else None
    repository.enqueue(
        1,
        "document_added",
        force=force,
        request_id=request_id,
    )
    claim = repository.claim("worker-a", NOW)
    assert claim is not None

    with pytest.raises(ValueError, match="bound identity"):
        repository.complete(
            claim,
            model_name="model",
            input_tokens=1,
            output_tokens=1,
            latency_ms=1,
        )

    renewed = repository.renew_lease(claim)
    assert renewed.id == claim.id


@pytest.mark.parametrize(
    ("transition", "expected_state"),
    [
        (lambda repository, claim: repository.fail(claim, JobError("fatal", "bad")), JobState.FAILED),
        (lambda repository, claim: repository.cancel(claim), JobState.CANCELLED),
        (lambda repository, claim: repository.supersede(claim), JobState.SUPERSEDED),
    ],
)
def test_non_success_terminal_transitions(repository, transition, expected_state):
    claim = claim_bound(repository)

    terminal = transition(repository, claim)

    assert terminal.state is expected_state
    assert terminal.completed_at == NOW
    assert terminal.lease_token is None


@pytest.mark.parametrize(
    "transition",
    [
        lambda repository, claim: repository.bind_content(
            claim, "hash-1", "profile-v1", NOW
        ),
        lambda repository, claim: repository.complete(
            claim,
            model_name="model",
            input_tokens=1,
            output_tokens=1,
            latency_ms=1,
        ),
        lambda repository, claim: repository.retry(
            claim, JobError("temporary", "retry")
        ),
        lambda repository, claim: repository.fail(claim, JobError("fatal", "fail")),
        lambda repository, claim: repository.cancel(claim),
        lambda repository, claim: repository.supersede(claim),
        lambda repository, claim: repository.renew_lease(claim),
    ],
)
def test_every_leased_mutation_rejects_the_wrong_token(repository, transition):
    repository.enqueue(1, "document_added")
    claim = repository.claim("worker-a", NOW)
    assert claim is not None
    stolen = replace(claim, lease_token=str(uuid4()))

    with pytest.raises(LostLeaseError):
        transition(repository, stolen)


def test_renew_lease_extends_from_current_time_without_incrementing_attempts(
    database, tmp_path
):
    repository = JobRepository(database, worker_config(tmp_path), clock=lambda: NOW)
    repository.enqueue(1, "document_added")
    claim = repository.claim("worker-a", NOW)
    assert claim is not None
    later = NOW + timedelta(seconds=60)
    later_repository = JobRepository(
        database, worker_config(tmp_path), clock=lambda: later
    )

    renewed = later_repository.renew_lease(claim)

    assert renewed.lease_expires_at == later + timedelta(seconds=300)
    assert renewed.attempt_count == 1
    assert renewed.cumulative_attempts == 1


@pytest.mark.parametrize(
    "transition",
    [
        lambda repository, claim: repository.bind_content(
            claim, "hash-2", "profile-v1", NOW
        ),
        lambda repository, claim: repository.complete(
            claim,
            model_name="model",
            input_tokens=1,
            output_tokens=1,
            latency_ms=1,
        ),
        lambda repository, claim: repository.retry(
            claim, JobError("temporary", "retry")
        ),
        lambda repository, claim: repository.fail(claim, JobError("fatal", "fail")),
        lambda repository, claim: repository.cancel(claim),
        lambda repository, claim: repository.supersede(claim),
        lambda repository, claim: repository.renew_lease(claim),
    ],
    ids=["bind", "complete", "retry", "fail", "cancel", "supersede", "renew"],
)
def test_each_leased_mutation_rejects_expiry(database, tmp_path, transition):
    repository = JobRepository(database, worker_config(tmp_path), clock=lambda: NOW)
    claim = claim_bound(repository)
    expired_repository = JobRepository(
        database,
        worker_config(tmp_path),
        clock=lambda: NOW + timedelta(seconds=300),
    )

    with pytest.raises(LostLeaseError):
        transition(expired_repository, claim)


def test_leased_mutation_checks_time_after_waiting_for_write_lock(tmp_path):
    path = tmp_path / "summary.sqlite3"
    setup_database = Database.open(path)
    setup_database.migrate()
    config = worker_config(tmp_path)
    setup_repository = JobRepository(setup_database, config, clock=lambda: NOW)
    setup_repository.enqueue(1, "document_added")
    claim = setup_repository.claim("worker-a", NOW)
    assert claim is not None
    setup_database.close()

    begin_attempted = threading.Event()
    lock_released = threading.Event()

    class SignalingConnection(sqlite3.Connection):
        def execute(self, sql, parameters=(), /):
            if sql.strip().startswith("BEGIN"):
                begin_attempted.set()
            return super().execute(sql, parameters)

    def renew_after_lock():
        connection = sqlite3.connect(
            path,
            isolation_level=None,
            timeout=5,
            factory=SignalingConnection,
        )
        connection.row_factory = sqlite3.Row
        database = Database(connection)
        repository = JobRepository(
            database,
            config,
            clock=lambda: (
                NOW + timedelta(seconds=300) if lock_released.is_set() else NOW
            ),
        )
        try:
            return repository.renew_lease(claim)
        finally:
            database.close()

    blocker = Database.open(path)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            with blocker.transaction(immediate=True):
                future = executor.submit(renew_after_lock)
                assert begin_attempted.wait(timeout=5)
                lock_released.set()

            with pytest.raises(LostLeaseError):
                future.result(timeout=10)
    finally:
        blocker.close()


def test_corrupt_claimed_row_raises_explicit_invariant_error(database, tmp_path):
    repository = JobRepository(database, worker_config(tmp_path), clock=lambda: NOW)
    repository.enqueue(1, "document_added")
    claim = repository.claim("worker-a", NOW)
    assert claim is not None
    database.connection.execute(
        "UPDATE jobs SET lease_owner = NULL WHERE id = ?", (claim.id,)
    )

    with pytest.raises(JobInvariantError, match="lease_owner"):
        repository.renew_lease(claim)


def test_retry_schedules_capped_exponential_delay_with_injected_jitter(
    database, tmp_path
):
    config = worker_config(
        tmp_path,
        retry_base_seconds=100,
        retry_max_seconds=300,
        retry_jitter_seconds=20,
    )
    repository = JobRepository(
        database, config, clock=lambda: NOW, uniform=lambda start, end: 20
    )
    repository.enqueue(1, "document_added")
    first_claim = repository.claim("worker-a", NOW)
    assert first_claim is not None

    first_retry = repository.retry(first_claim, JobError("temporary", "first"))

    assert first_retry.state is JobState.RETRY_WAIT
    assert first_retry.next_attempt_at == NOW + timedelta(seconds=120)
    assert repository.claim("worker-a", NOW + timedelta(seconds=119)) is None
    second_claim = repository.claim("worker-a", NOW + timedelta(seconds=120))
    assert second_claim is not None
    second_retry = JobRepository(
        database,
        config,
        clock=lambda: NOW + timedelta(seconds=120),
        uniform=lambda start, end: 20,
    ).retry(second_claim, JobError("temporary", "second"))
    assert second_retry.next_attempt_at == NOW + timedelta(seconds=340)

    third_claim = repository.claim("worker-a", NOW + timedelta(seconds=340))
    assert third_claim is not None
    capped = JobRepository(
        database,
        config,
        clock=lambda: NOW + timedelta(seconds=340),
        uniform=lambda start, end: 20,
    ).retry(third_claim, JobError("temporary", "third"))
    assert capped.next_attempt_at == NOW + timedelta(seconds=640)


def test_new_delivery_wakes_an_unbound_job_waiting_to_retry(database, tmp_path):
    repository = JobRepository(
        database,
        worker_config(tmp_path),
        clock=lambda: NOW,
        uniform=lambda start, end: 0,
    )
    pending = repository.enqueue(1, "document_added")
    claim = repository.claim("worker-a", NOW)
    assert claim is not None
    waiting = repository.retry(claim, JobError("temporary", "fetch failed"))
    assert waiting.state is JobState.RETRY_WAIT
    assert waiting.content_hash is None

    coalesced = repository.enqueue(1, "document_updated")

    assert coalesced.id == pending.id
    assert coalesced.state is JobState.PENDING
    assert coalesced.next_attempt_at is None


def test_retry_budget_exhaustion_fails_on_the_fixed_epoch_limit(database, tmp_path):
    config = worker_config(tmp_path, max_attempts=2)
    repository = JobRepository(
        database, config, clock=lambda: NOW, uniform=lambda start, end: 0
    )
    repository.enqueue(1, "document_added")
    first = repository.claim("worker-a", NOW)
    assert first is not None
    repository.retry(first, JobError("temporary", "first"))
    second = repository.claim("worker-a", NOW + timedelta(seconds=5))
    assert second is not None

    exhausted = JobRepository(
        database, config, clock=lambda: NOW + timedelta(seconds=5)
    ).retry(second, JobError("temporary", "second"))

    assert exhausted.state is JobState.FAILED
    assert exhausted.next_attempt_at is None
    assert exhausted.attempt_count == 2
    assert exhausted.cumulative_attempts == 2


def test_permanent_failure_and_deleted_document_do_not_retry(repository):
    failed_claim = claim_bound(repository, document_id=1)
    failed = repository.fail(failed_claim, JobError("permanent", "unsupported"))
    cancelled_claim = claim_bound(repository, document_id=2)
    cancelled = repository.cancel(cancelled_claim)

    assert failed.state is JobState.FAILED
    assert failed.next_attempt_at is None
    assert cancelled.state is JobState.CANCELLED
    assert cancelled.next_attempt_at is None
    assert repository.claim("worker-b", NOW + timedelta(days=1)) is None


def test_manual_retry_starts_fresh_epoch_and_preserves_cumulative_history(
    database, tmp_path
):
    config = worker_config(tmp_path, max_attempts=1)
    repository = JobRepository(
        database, config, clock=lambda: NOW, uniform=lambda start, end: 0
    )
    claim = claim_bound(repository)
    failed = repository.retry(claim, JobError("temporary", "safe failure"))
    assert failed.state is JobState.FAILED

    manual = repository.manual_retry(failed.id)

    assert manual.state is JobState.PENDING
    assert manual.retry_epoch == 1
    assert manual.attempt_count == 0
    assert manual.cumulative_attempts == 1
    assert manual.error_history == failed.error_history
    assert manual.last_error_class == "temporary"
    assert manual.completed_at is None
    retried_claim = repository.claim("worker-b", NOW)
    assert retried_claim is not None
    assert retried_claim.attempt_count == 1
    assert retried_claim.cumulative_attempts == 2


def test_manual_retry_applies_only_to_failed_jobs(repository):
    pending = repository.enqueue(1, "document_added")

    with pytest.raises(ValueError, match="failed"):
        repository.manual_retry(pending.id)


@pytest.mark.parametrize("successor_state", [JobState.PENDING, JobState.RETRY_WAIT])
def test_manual_retry_conflicts_without_mutating_unresolved_normal_successor(
    database, tmp_path, successor_state
):
    config = worker_config(tmp_path, max_attempts=2)
    repository = JobRepository(
        database, config, clock=lambda: NOW, uniform=lambda start, end: 0
    )
    failed_job = repository.enqueue(1, "document_added")
    claim = repository.claim("worker-a", NOW)
    assert claim is not None
    failed = repository.fail(claim, JobError("permanent", "failed original"))
    assert failed.state is JobState.FAILED
    successor = repository.enqueue(1, "document_updated")
    if successor_state is JobState.RETRY_WAIT:
        successor_claim = repository.claim("worker-b", NOW)
        assert successor_claim is not None
        successor = repository.retry(
            successor_claim, JobError("temporary", "prior retry history")
        )
        assert successor.error_history

    with pytest.raises(ManualRetryConflictError):
        repository.manual_retry(failed_job.id)
    with pytest.raises(ManualRetryConflictError):
        repository.manual_retry(failed_job.id)

    claim_time = (
        NOW
        if successor_state is JobState.PENDING
        else NOW + timedelta(seconds=5)
    )
    claimed = repository.claim("worker-c", claim_time)
    assert claimed is not None
    assert claimed.id == successor.id
    assert claimed.error_history == successor.error_history


def test_error_history_strips_controls_bounds_messages_and_keeps_safe_code(repository):
    claim = claim_bound(repository)
    unsafe_message = "visible\nsecret\x00" + "x" * 600

    failed = repository.fail(
        claim, JobError("PaperlessError", unsafe_message, code="document-missing")
    )

    assert failed.last_error_class == "PaperlessError"
    assert failed.last_error_message is not None
    assert "\n" not in failed.last_error_message
    assert "\x00" not in failed.last_error_message
    assert len(failed.last_error_message) == 500
    assert failed.error_history == (
        {
            "at": "2026-08-27T12:00:00+00:00",
            "class": "PaperlessError",
            "message": failed.last_error_message,
            "code": "document-missing",
        },
    )


@pytest.mark.parametrize(
    "error",
    [
        JobError("bad class", "message"),
        JobError("safe", "message", code="bad code"),
    ],
)
def test_error_records_reject_unsafe_class_and_code(repository, error):
    claim = claim_bound(repository)

    with pytest.raises(ValueError, match="safe identifier"):
        repository.fail(claim, error)


def test_manual_retry_refuses_malformed_error_history_without_overwriting(
    database, tmp_path
):
    repository = JobRepository(database, worker_config(tmp_path), clock=lambda: NOW)
    claim = claim_bound(repository)
    failed = repository.fail(claim, JobError("fatal", "safe"))
    database.connection.execute(
        "UPDATE jobs SET error_history = ? WHERE id = ?", ("not-json", failed.id)
    )

    with pytest.raises(ValueError, match="error history"):
        repository.manual_retry(failed.id)

    row = database.connection.execute(
        "SELECT state, error_history FROM jobs WHERE id = ?", (failed.id,)
    ).fetchone()
    assert tuple(row) == ("failed", "not-json")


def test_manual_retry_refuses_invalid_error_history_records(database, tmp_path):
    repository = JobRepository(database, worker_config(tmp_path), clock=lambda: NOW)
    claim = claim_bound(repository)
    failed = repository.fail(claim, JobError("fatal", "safe"))
    invalid_history = '[{"class":"fatal","message":["not text"]}]'
    database.connection.execute(
        "UPDATE jobs SET error_history = ? WHERE id = ?",
        (invalid_history, failed.id),
    )

    with pytest.raises(ValueError, match="error history"):
        repository.manual_retry(failed.id)


def test_retry_uses_durable_attempts_and_history_not_the_claim_snapshot(
    database, tmp_path
):
    repository = JobRepository(
        database,
        worker_config(tmp_path),
        clock=lambda: NOW,
        uniform=lambda start, end: 0,
    )
    repository.enqueue(1, "document_added")
    claim = repository.claim("worker-a", NOW)
    assert claim is not None
    altered = replace(
        claim,
        attempt_count=99,
        cumulative_attempts=99,
        error_history=({"class": "forged"},),
    )

    retried = repository.retry(altered, JobError("temporary", "real"))

    assert retried.state is JobState.RETRY_WAIT
    assert retried.attempt_count == 1
    assert retried.cumulative_attempts == 1
    assert len(retried.error_history) == 1
    assert retried.error_history[0]["message"] == "real"


def test_completion_resolves_bound_identity_race_as_supersession(repository):
    repository.enqueue(1, "document_added")
    first = repository.claim("worker-a", NOW)
    assert first is not None
    repository.enqueue(1, "document_updated")
    second = repository.claim("worker-b", NOW)
    assert second is not None
    first_bound = repository.bind_content(first, "hash-1", "profile-v1", NOW).job
    second_bound = repository.bind_content(second, "hash-1", "profile-v1", NOW).job
    completed = repository.complete(
        first_bound,
        model_name="model",
        input_tokens=1,
        output_tokens=1,
        latency_ms=1,
    )

    raced = repository.complete(
        second_bound,
        model_name="model",
        input_tokens=1,
        output_tokens=1,
        latency_ms=1,
    )

    assert completed.state is JobState.COMPLETED
    assert raced.state is JobState.SUPERSEDED


def test_only_one_terminal_transition_wins_across_two_connections(tmp_path):
    path = tmp_path / "summary.sqlite3"
    setup_database = Database.open(path)
    setup_database.migrate()
    config = worker_config(tmp_path)
    repository = JobRepository(setup_database, config, clock=lambda: NOW)
    claim = claim_bound(repository)
    setup_database.close()
    barrier = threading.Barrier(2)

    def complete():
        database = Database.open(path)
        try:
            local = JobRepository(database, config, clock=lambda: NOW)
            barrier.wait(timeout=5)
            return local.complete(
                claim,
                model_name="model",
                input_tokens=1,
                output_tokens=1,
                latency_ms=1,
            )
        except LostLeaseError:
            return None
        finally:
            database.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [
            future.result(timeout=10)
            for future in [executor.submit(complete), executor.submit(complete)]
        ]

    assert sum(result is not None for result in results) == 1
