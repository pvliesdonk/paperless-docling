from __future__ import annotations

import asyncio
import json
import secrets
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from paperless_docling_worker import api
from paperless_docling_worker import metrics as worker_metrics_module
from paperless_docling_worker.api import create_app
from paperless_docling_worker.config import WorkerConfig
from paperless_docling_worker.db import Database
from paperless_docling_worker.jobs import JobMetrics, JobRepository, JobState
from paperless_docling_worker.metrics import WorkerMetrics
from pydantic import SecretStr


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
        paperless_token=SecretStr("paperless-secret"),
        webhook_token=SecretStr("webhook-secret"),
        llm_api_key=SecretStr("llm-secret"),
    )
    return replace(config, **overrides)


def open_test_database(path: Path) -> Database:
    connection = sqlite3.connect(
        path,
        isolation_level=None,
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row
    database = Database(connection)
    database.migrate()
    return database


@pytest.fixture
def repository(tmp_path):
    database = open_test_database(tmp_path / "summary.sqlite3")
    try:
        yield JobRepository(database, worker_config(tmp_path))
    finally:
        database.close()


@pytest.fixture
def client(tmp_path, repository):
    with TestClient(create_app(worker_config(tmp_path), repository)) as test_client:
        yield test_client


def auth_headers(token: str = "webhook-secret") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "webhook-secret"},
        {"Authorization": "Basic webhook-secret"},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer webhook-secret extra"},
        auth_headers("wrong-secret"),
    ],
)
def test_webhook_rejects_missing_malformed_and_wrong_credentials_identically(
    client, headers
):
    response = client.post(
        "/v1/events/paperless",
        headers=headers,
        json={"document_id": 41, "event": "document_added"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}
    assert response.headers["www-authenticate"] == "Bearer"


def test_webhook_rejects_duplicate_authorization_credentials(client):
    response = client.post(
        "/v1/events/paperless",
        headers=[
            ("Authorization", "Bearer webhook-secret"),
            ("Authorization", "Bearer webhook-secret"),
            ("Content-Type", "application/json"),
        ],
        content=b'{"document_id":41,"event":"document_added"}',
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


@pytest.mark.parametrize(
    "token",
    [
        b"webhook secret",
        b"webhook\tsecret",
        b"webhook\rsecret",
        b"webhook\nsecret",
        b"webhook\vsecret",
        b"webhook\fsecret",
        b"webhook-secret\n\n",
        b"webhook-secret\r\n\r\n",
    ],
)
def test_authentication_rejects_whitespace_and_repeated_line_endings(token):
    assert api._valid_credential(
        [b"Bearer " + token], sha256(token).digest()
    ) is False


@pytest.mark.parametrize("token", [b"abc-XYZ_123.~+/=", b"punctuation:!@#$%^&*()"])
def test_authentication_accepts_non_whitespace_credential_characters(token):
    assert api._valid_credential(
        [b"Bearer " + token], sha256(token).digest()
    ) is True


def test_body_middleware_does_not_override_non_post_routing(client):
    response = client.get("/v1/events/paperless")

    assert response.status_code == 405


@pytest.mark.parametrize(
    ("headers", "expected_status"),
    [
        ({}, 401),
        ({"Authorization": "Basic malformed"}, 401),
        (
            [
                ("Authorization", "Bearer webhook-secret"),
                ("Authorization", "Bearer webhook-secret"),
            ],
            401,
        ),
        (auth_headers("wrong"), 401),
        (auth_headers(), 202),
    ],
    ids=["missing", "malformed", "duplicate", "wrong", "success"],
)
def test_every_auth_path_performs_one_equal_length_digest_comparison(
    tmp_path, repository, monkeypatch, headers, expected_status
):
    comparisons = []

    def observe(received: bytes, expected: bytes) -> bool:
        comparisons.append((received, expected))
        return secrets.compare_digest(received, expected)

    monkeypatch.setattr(api, "_tokens_match", observe)
    with TestClient(create_app(worker_config(tmp_path), repository)) as test_client:
        response = test_client.post(
            "/v1/events/paperless",
            headers=headers,
            json={"document_id": 41, "event": "document_added"},
        )

    assert response.status_code == expected_status
    assert len(comparisons) == 1
    received, expected = comparisons[0]
    assert len(received) == len(expected) == 32
    assert expected != b"webhook-secret"
    if expected_status == 401:
        assert response.json() == {"detail": "Unauthorized"}


def test_wrong_token_returns_401_before_malformed_json_is_validated(client):
    response = client.post(
        "/v1/events/paperless",
        headers={**auth_headers("wrong"), "Content-Type": "application/json"},
        content=b"not-json",
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


@pytest.mark.parametrize(
    "payload",
    [
        {"document_id": 0, "event": "document_added"},
        {"document_id": -1, "event": "document_added"},
        {"document_id": True, "event": "document_added"},
        {"document_id": 1.0, "event": "document_added"},
        {"document_id": "1", "event": "document_added"},
        {"document_id": 1, "event": "document_deleted"},
        {
            "document_id": 1,
            "event": "document_added",
            "extra": "private rejected input",
        },
    ],
)
def test_webhook_schema_rejects_invalid_values_without_enqueueing(
    client, repository, payload
):
    response = client.post(
        "/v1/events/paperless", headers=auth_headers(), json=payload
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid request"}
    assert "private rejected input" not in response.text
    assert repository.claim("test-worker", datetime.now(UTC)) is None


@pytest.mark.parametrize(
    ("content_type", "body", "status_code"),
    [
        ("text/plain", b'{"document_id":1,"event":"document_added"}', 415),
        ("application/json", b"not-json", 422),
    ],
)
def test_webhook_rejects_non_json_and_malformed_json(
    client, repository, content_type, body, status_code
):
    response = client.post(
        "/v1/events/paperless",
        headers={**auth_headers(), "Content-Type": content_type},
        content=body,
    )

    assert response.status_code == status_code
    if status_code == 422:
        assert response.json() == {"detail": "Invalid request"}
        assert "not-json" not in response.text
    assert repository.claim("test-worker", datetime.now(UTC)) is None


def test_webhook_returns_accepted_only_after_a_durable_enqueue(
    client, repository
):
    response = client.post(
        "/v1/events/paperless",
        headers=auth_headers(),
        json={"document_id": 41, "event": "document_updated"},
    )

    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}
    claimed = repository.claim("test-worker", datetime.now(UTC))
    assert claimed is not None
    assert claimed.document_id == 41
    assert claimed.event == "document_updated"
    assert claimed.state is JobState.LEASED


def test_accepted_enqueue_is_visible_after_reopening_database(tmp_path):
    path = tmp_path / "durable.sqlite3"
    database = Database.open(path)
    database.migrate()
    repository = JobRepository(database, worker_config(tmp_path))
    with TestClient(create_app(worker_config(tmp_path), repository)) as test_client:
        response = test_client.post(
            "/v1/events/paperless",
            headers=auth_headers(),
            json={"document_id": 41, "event": "document_updated"},
        )
    database.close()

    reopened = Database.open(path)
    try:
        durable = JobRepository(reopened, worker_config(tmp_path)).claim(
            "independent-worker", datetime.now(UTC)
        )
    finally:
        reopened.close()

    assert response.status_code == 202
    assert durable is not None
    assert durable.document_id == 41
    assert durable.event == "document_updated"


def test_commit_failure_returns_503_and_rolls_back_enqueue(tmp_path):
    class CommitFailingConnection(sqlite3.Connection):
        fail_commit = False

        def commit(self):
            if self.fail_commit:
                raise sqlite3.OperationalError("secret-like commit detail")
            return super().commit()

    path = tmp_path / "commit-failure.sqlite3"
    connection = sqlite3.connect(
        path,
        isolation_level=None,
        check_same_thread=False,
        factory=CommitFailingConnection,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    database = Database(connection)
    database.migrate()
    connection.fail_commit = True
    repository = JobRepository(database, worker_config(tmp_path))
    try:
        with TestClient(create_app(worker_config(tmp_path), repository)) as test_client:
            response = test_client.post(
                "/v1/events/paperless",
                headers=auth_headers(),
                json={"document_id": 41, "event": "document_added"},
            )
        assert connection.in_transaction is False
        with sqlite3.connect(path) as verifier:
            assert verifier.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    finally:
        database.close()

    assert response.status_code == 503
    assert response.json() == {"detail": "Service unavailable"}
    assert "secret-like" not in response.text


def test_webhook_translates_repository_errors_to_a_generic_503(tmp_path, caplog):
    class FailingRepository:
        def enqueue(self, document_id, event):
            raise sqlite3.OperationalError(
                "database /private/summary.sqlite3 contains document 41"
            )

        def metrics_snapshot(self):
            return JobMetrics(
                state_totals={state: 0 for state in JobState},
                ready_queue_depth=0,
                oldest_eligible_age_seconds=0,
            )

    with TestClient(
        create_app(worker_config(tmp_path), FailingRepository())  # type: ignore[arg-type]
    ) as test_client:
        response = test_client.post(
            "/v1/events/paperless",
            headers=auth_headers(),
            json={"document_id": 41, "event": "document_added"},
        )
        metrics = test_client.get("/metrics").text

    assert response.status_code == 503
    assert response.json() == {"detail": "Service unavailable"}
    assert "/private" not in response.text
    assert "document 41" not in response.text
    assert 'paperless_docling_enqueue_total{outcome="unavailable"} 1.0' in metrics
    record = next(record for record in caplog.records if record.message == "job enqueue failed")
    assert record.document_id == 41
    assert record.event == "document_added"
    assert "/private" not in record.message


async def asgi_request(app, *, headers, messages):
    sent = []
    pending = iter(messages)

    async def receive():
        return next(pending)

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/events/paperless",
        "raw_path": b"/v1/events/paperless",
        "query_string": b"",
        "headers": headers,
        "client": ("testclient", 123),
        "server": ("testserver", 80),
        "root_path": "",
    }
    await app(scope, receive, send)
    status = next(message["status"] for message in sent if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    return status, json.loads(body)


def raw_webhook_headers(content_length: int | None = None):
    headers = [
        (b"authorization", b"Bearer webhook-secret"),
        (b"content-type", b"application/json"),
    ]
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode("ascii")))
    return headers


def test_body_middleware_replays_one_canonical_message_for_fragmented_input():
    body = b'{"document_id":41,"event":"document_added"}'
    replayed = []

    async def downstream(scope, receive, send):
        while True:
            message = await receive()
            replayed.append(message)
            if message["type"] != "http.request" or not message.get(
                "more_body", False
            ):
                break
        await JSONResponse({"status": "captured"})(scope, receive, send)

    messages = (
        *(
            {"type": "http.request", "body": b"", "more_body": True}
            for _ in range(10_000)
        ),
        *(
            {
                "type": "http.request",
                "body": bytes([byte]),
                "more_body": index < len(body) - 1,
            }
            for index, byte in enumerate(body)
        ),
    )
    middleware = api._WebhookRequestMiddleware(
        downstream,
        max_bytes=len(body),
        webhook_token_digest=sha256(b"webhook-secret").digest(),
    )

    status, response = asyncio.run(
        asgi_request(
            middleware,
            headers=raw_webhook_headers(len(body)),
            messages=messages,
        )
    )

    assert status == 200
    assert response == {"status": "captured"}
    assert replayed == [
        {"type": "http.request", "body": body, "more_body": False}
    ]


def test_valid_fragmented_body_with_empty_chunks_enqueues(tmp_path, repository):
    body = b'{"document_id":41,"event":"document_added"}'
    messages = [
        {"type": "http.request", "body": b"", "more_body": True},
        {"type": "http.request", "body": body[:7], "more_body": True},
        {"type": "http.request", "body": b"", "more_body": True},
        {"type": "http.request", "body": body[7:], "more_body": False},
    ]

    status, response = asyncio.run(
        asgi_request(
            create_app(worker_config(tmp_path), repository),
            headers=raw_webhook_headers(len(body)),
            messages=messages,
        )
    )

    assert status == 202
    assert response == {"status": "accepted"}
    claim = repository.claim("test-worker", datetime.now(UTC))
    assert claim is not None
    assert claim.document_id == 41


@pytest.mark.parametrize(
    ("declared_length", "body"),
    [
        (1, b"{}"),
        (3, b"{}"),
    ],
    ids=["underreported", "overreported"],
)
def test_declared_length_must_match_actual_body(
    tmp_path, repository, declared_length, body
):
    status, response = asyncio.run(
        asgi_request(
            create_app(worker_config(tmp_path), repository),
            headers=raw_webhook_headers(declared_length),
            messages=[
                {"type": "http.request", "body": body, "more_body": False}
            ],
        )
    )

    assert status == 400
    assert response == {"detail": "Invalid request body"}
    assert repository.claim("test-worker", datetime.now(UTC)) is None


def test_disconnect_before_complete_body_is_rejected_without_enqueue(
    tmp_path, repository
):
    status, response = asyncio.run(
        asgi_request(
            create_app(worker_config(tmp_path), repository),
            headers=raw_webhook_headers(50),
            messages=[
                {"type": "http.request", "body": b"{", "more_body": True},
                {"type": "http.disconnect"},
            ],
        )
    )

    assert status == 400
    assert response == {"detail": "Invalid request body"}
    assert repository.claim("test-worker", datetime.now(UTC)) is None


def test_body_limit_rejects_declared_oversize_without_reading_the_body(
    tmp_path, repository
):
    received = False

    async def run():
        nonlocal received

        async def receive():
            nonlocal received
            received = True
            return {"type": "http.request", "body": b"ignored"}

        app = create_app(worker_config(tmp_path, max_request_bytes=10), repository)
        sent = []
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/events/paperless",
            "raw_path": b"/v1/events/paperless",
            "query_string": b"",
            "headers": [
                (b"authorization", b"Bearer webhook-secret"),
                (b"content-type", b"application/json"),
                (b"content-length", b"11"),
            ],
            "client": ("testclient", 123),
            "server": ("testserver", 80),
            "root_path": "",
        }

        async def send(message):
            sent.append(message)

        await app(scope, receive, send)
        return next(
            message["status"]
            for message in sent
            if message["type"] == "http.response.start"
        )

    assert asyncio.run(run()) == 413
    assert received is False


def test_body_limit_stops_chunked_body_before_receiving_later_chunks(
    tmp_path, repository
):
    consumed = 0

    async def run():
        nonlocal consumed
        chunks = iter(
            [
                {"type": "http.request", "body": b"123456", "more_body": True},
                {"type": "http.request", "body": b"78901", "more_body": True},
                {"type": "http.request", "body": b"must-not-read"},
            ]
        )

        async def receive():
            nonlocal consumed
            consumed += 1
            return next(chunks)

        app = create_app(worker_config(tmp_path, max_request_bytes=10), repository)
        sent = []
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/events/paperless",
            "raw_path": b"/v1/events/paperless",
            "query_string": b"",
            "headers": [
                (b"authorization", b"Bearer webhook-secret"),
                (b"content-type", b"application/json"),
            ],
            "client": ("testclient", 123),
            "server": ("testserver", 80),
            "root_path": "",
        }

        async def send(message):
            sent.append(message)

        await app(scope, receive, send)
        return next(
            message["status"]
            for message in sent
            if message["type"] == "http.response.start"
        )

    assert asyncio.run(run()) == 413
    assert consumed == 2


@pytest.mark.parametrize(
    "content_lengths",
    [[b"invalid"], [b"10", b"11"], [b"-1"]],
)
def test_body_limit_rejects_invalid_or_conflicting_content_length(
    tmp_path, repository, content_lengths
):
    headers = [
        (b"authorization", b"Bearer webhook-secret"),
        (b"content-type", b"application/json"),
        *((b"content-length", value) for value in content_lengths),
    ]
    status, body = asyncio.run(
        asgi_request(
            create_app(worker_config(tmp_path), repository),
            headers=headers,
            messages=[{"type": "http.request", "body": b""}],
        )
    )

    assert status == 400
    assert body == {"detail": "Invalid Content-Length"}


def test_liveness_reports_only_process_status(client):
    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_liveness_does_not_touch_repository_or_readiness_dependencies(tmp_path):
    touched = []

    class BombRepository:
        def check_ready(self):
            touched.append("database")
            raise AssertionError

        def metrics_snapshot(self):
            touched.append("metrics")
            raise AssertionError

        def enqueue(self, document_id, event):
            touched.append("enqueue")
            raise AssertionError

    def bomb_summary_readiness():
        touched.append("summary")
        raise AssertionError

    app = create_app(
        worker_config(tmp_path),
        BombRepository(),  # type: ignore[arg-type]
        summary_field_readiness=bomb_summary_readiness,
    )
    with TestClient(app) as test_client:
        response = test_client.get("/health/live")

    assert response.status_code == 200
    assert touched == []


def test_blocked_enqueue_does_not_prevent_concurrent_liveness(tmp_path):
    enqueue_entered = threading.Event()
    release_enqueue = threading.Event()

    class BlockingRepository:
        def enqueue(self, document_id, event):
            enqueue_entered.set()
            assert release_enqueue.wait(timeout=2)

    app = create_app(
        worker_config(tmp_path), BlockingRepository()  # type: ignore[arg-type]
    )
    with TestClient(app) as test_client, ThreadPoolExecutor(
        max_workers=2
    ) as executor:
        enqueue = executor.submit(
            test_client.post,
            "/v1/events/paperless",
            headers=auth_headers(),
            json={"document_id": 41, "event": "document_added"},
        )
        assert enqueue_entered.wait(timeout=2)
        liveness = executor.submit(test_client.get, "/health/live")
        try:
            live_response = liveness.result(timeout=0.25)
        finally:
            release_enqueue.set()
        enqueue_response = enqueue.result(timeout=2)

    assert live_response.status_code == 200
    assert enqueue_response.status_code == 202


def test_sync_enqueue_works_with_database_open_connection(tmp_path):
    database = Database.open(tmp_path / "threaded.sqlite3")
    database.migrate()
    repository = JobRepository(database, worker_config(tmp_path))
    try:
        with TestClient(create_app(worker_config(tmp_path), repository)) as test_client:
            response = test_client.post(
                "/v1/events/paperless",
                headers=auth_headers(),
                json={"document_id": 41, "event": "document_added"},
            )
    finally:
        database.close()

    assert response.status_code == 202


def test_readiness_does_not_claim_unconfigured_summary_field_validation(
    client,
):
    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "components": {"database": "ready", "summary_field": "not_checked"},
    }


def test_readiness_uses_real_writable_current_database(tmp_path, repository):
    app = create_app(
        worker_config(tmp_path),
        repository,
        summary_field_readiness=lambda: True,
    )

    with TestClient(app) as test_client:
        response = test_client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "components": {"database": "ready", "summary_field": "ready"},
    }


def test_readiness_rejects_real_read_only_database(tmp_path):
    path = tmp_path / "read-only.sqlite3"
    setup = Database.open(path)
    setup.migrate()
    setup.close()
    connection = sqlite3.connect(
        f"file:{path}?mode=ro",
        uri=True,
        isolation_level=None,
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row
    database = Database(connection)
    repository = JobRepository(database, worker_config(tmp_path))
    try:
        with TestClient(
            create_app(
                worker_config(tmp_path),
                repository,
                summary_field_readiness=lambda: True,
            )
        ) as test_client:
            response = test_client.get("/health/ready")
    finally:
        database.close()

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "components": {"database": "not_ready", "summary_field": "ready"},
    }


def test_readiness_rejects_noncurrent_schema_without_exposing_details(
    tmp_path,
):
    database = open_test_database(tmp_path / "old-schema.sqlite3")
    database.connection.execute("PRAGMA user_version = 0")
    repository = JobRepository(database, worker_config(tmp_path))
    app = create_app(
        worker_config(tmp_path),
        repository,
        summary_field_readiness=lambda: True,
    )

    try:
        with TestClient(app) as test_client:
            response = test_client.get("/health/ready")
    finally:
        database.close()

    assert response.status_code == 503
    assert response.json()["components"]["database"] == "not_ready"
    assert "version" not in response.text.lower()
    assert str(tmp_path) not in response.text


def test_readiness_hides_summary_check_exceptions(tmp_path, repository):
    def fail_summary_check():
        raise RuntimeError(
            "https://admin:secret@paperless.example.test/api/fields traceback"
        )

    app = create_app(
        worker_config(tmp_path),
        repository,
        summary_field_readiness=fail_summary_check,
    )

    with TestClient(app) as test_client:
        response = test_client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "components": {"database": "ready", "summary_field": "not_ready"},
    }
    assert "secret" not in response.text
    assert "traceback" not in response.text.lower()


def test_enqueue_does_not_construct_outbound_clients(
    tmp_path, repository
):
    constructed = []

    class PaperlessClient:
        def __init__(self):
            constructed.append("paperless")
            raise AssertionError("Paperless client constructed during enqueue")

    class LlmClient:
        def __init__(self):
            constructed.append("llm")
            raise AssertionError("LLM client constructed during enqueue")

    def future_readiness_check():
        PaperlessClient()
        LlmClient()
        return True

    app = create_app(
        worker_config(tmp_path),
        repository,
        summary_field_readiness=future_readiness_check,
    )

    with TestClient(app) as test_client:
        response = test_client.post(
            "/v1/events/paperless",
            headers=auth_headers(),
            json={"document_id": 41, "event": "document_added"},
        )

    assert response.status_code == 202
    assert constructed == []


def test_metrics_report_enqueue_outcomes_and_current_queue_state(tmp_path):
    now = datetime(2026, 8, 27, 12, tzinfo=UTC)
    database = open_test_database(tmp_path / "metrics.sqlite3")
    repository = JobRepository(database, worker_config(tmp_path), clock=lambda: now)
    repository.enqueue(1, "document_added")
    now += timedelta(seconds=10)
    repository.enqueue(2, "document_updated")
    claim = repository.claim("worker-a", now)
    assert claim is not None
    app = create_app(worker_config(tmp_path), repository)
    try:
        with TestClient(app) as test_client:
            accepted = test_client.post(
                "/v1/events/paperless",
                headers=auth_headers(),
                json={"document_id": 3, "event": "document_added"},
            )
            now += timedelta(seconds=10)
            response = test_client.get("/metrics")
    finally:
        database.close()

    assert accepted.status_code == 202
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert 'paperless_docling_enqueue_total{outcome="accepted"} 1.0' in response.text
    assert 'paperless_docling_jobs{state="leased"} 1.0' in response.text
    assert 'paperless_docling_jobs{state="pending"} 2.0' in response.text
    assert "paperless_docling_ready_queue_depth 2.0" in response.text
    assert "paperless_docling_oldest_eligible_age_seconds 10.0" in response.text


def test_metrics_registry_is_isolated_per_application(tmp_path):
    databases = [
        open_test_database(tmp_path / f"metrics-{number}.sqlite3")
        for number in range(2)
    ]
    repositories = [
        JobRepository(database, worker_config(tmp_path)) for database in databases
    ]
    apps = [
        create_app(worker_config(tmp_path), repository)
        for repository in repositories
    ]
    try:
        with TestClient(apps[0]) as first, TestClient(apps[1]) as second:
            assert first.post(
                "/v1/events/paperless",
                headers=auth_headers(),
                json={"document_id": 1, "event": "document_added"},
            ).status_code == 202
            first_metrics = first.get("/metrics").text
            second_metrics = second.get("/metrics").text
    finally:
        for database in databases:
            database.close()

    assert 'paperless_docling_enqueue_total{outcome="accepted"} 1.0' in first_metrics
    assert 'paperless_docling_enqueue_total{outcome="accepted"} 0.0' in second_metrics


def test_metrics_snapshot_failure_is_generic_in_response_and_logs(tmp_path, caplog):
    class FailingMetricsRepository:
        def metrics_snapshot(self):
            raise sqlite3.OperationalError(
                "token=secret-value at /private/summary.sqlite3"
            )

    app = create_app(
        worker_config(tmp_path),
        FailingMetricsRepository(),  # type: ignore[arg-type]
    )
    with TestClient(app) as test_client:
        response = test_client.get("/metrics")

    assert response.status_code == 503
    assert response.json() == {"detail": "Service unavailable"}
    assert "secret-value" not in response.text
    assert "/private" not in response.text
    records = [
        record for record in caplog.records if record.message == "metrics snapshot failed"
    ]
    assert len(records) == 1
    assert "secret-value" not in records[0].message
    assert "/private" not in records[0].message


def test_concurrent_metric_renders_do_not_interleave_gauge_snapshots(monkeypatch):
    worker_metrics = WorkerMetrics()
    first_entered = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()
    guard = threading.Lock()
    active = 0
    overlap = False
    call_count = 0

    def blocked_generate_latest(registry):
        nonlocal active, overlap, call_count
        with guard:
            call_count += 1
            current_call = call_count
            active += 1
            overlap = overlap or active > 1
        if current_call == 1:
            first_entered.set()
            assert release_first.wait(timeout=2)
        else:
            second_entered.set()
        with guard:
            active -= 1
        return b"metrics"

    monkeypatch.setattr(
        worker_metrics_module, "generate_latest", blocked_generate_latest
    )
    snapshots = [
        JobMetrics({state: index for state in JobState}, index, float(index))
        for index in (1, 2)
    ]

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(worker_metrics.render, snapshots[0])
        assert first_entered.wait(timeout=2)
        second = executor.submit(worker_metrics.render, snapshots[1])
        second_entered.wait(timeout=0.1)
        release_first.set()
        first.result(timeout=2)
        second.result(timeout=2)

    assert overlap is False


def test_metrics_use_only_bounded_state_and_outcome_labels(tmp_path, repository):
    app = create_app(worker_config(tmp_path), repository)
    with TestClient(app) as test_client:
        metrics = test_client.get("/metrics").text

    label_lines = [line for line in metrics.splitlines() if "{" in line]
    assert label_lines
    assert all(
        line.startswith("paperless_docling_enqueue_total{outcome=")
        or line.startswith("paperless_docling_enqueue_created{outcome=")
        or line.startswith("paperless_docling_jobs{state=")
        for line in label_lines
    )
    assert "document_id" not in metrics
    assert "paperless.example.test" not in metrics
    assert "summary.sqlite3" not in metrics
