from __future__ import annotations

import logging
import secrets
from collections.abc import Callable
from hashlib import sha256
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, StrictInt
from starlette.types import Receive, Scope, Send

from .config import WorkerConfig
from .credentials import is_valid_credential
from .jobs import JobRepository
from .metrics import WorkerMetrics

logger = logging.getLogger(__name__)

_WEBHOOK_PATH = "/v1/events/paperless"
_AUTHORIZATION = b"authorization"
_CONTENT_LENGTH = b"content-length"
_CONTENT_TYPE = b"content-type"


class _WebhookRequestMiddleware:
    def __init__(self, app, *, max_bytes: int, webhook_token_digest: bytes) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.webhook_token_digest = webhook_token_digest

    async def __call__(self, scope, receive, send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or scope.get("path") != _WEBHOOK_PATH
        ):
            await self.app(scope, receive, send)
            return

        headers = scope.get("headers", ())
        credentials = [value for name, value in headers if name == _AUTHORIZATION]
        if not _valid_credential(credentials, self.webhook_token_digest):
            response = JSONResponse(
                {"detail": "Unauthorized"},
                status_code=status.HTTP_401_UNAUTHORIZED,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return

        lengths = [value for name, value in headers if name == _CONTENT_LENGTH]
        try:
            parsed_lengths = [_parse_content_length(value) for value in lengths]
        except ValueError:
            await _json_error(scope, receive, send, 400, "Invalid Content-Length")
            return
        if parsed_lengths and len(set(parsed_lengths)) != 1:
            await _json_error(scope, receive, send, 400, "Invalid Content-Length")
            return
        if parsed_lengths and parsed_lengths[0] > self.max_bytes:
            await _json_error(scope, receive, send, 413, "Request body too large")
            return

        content_types = [value for name, value in headers if name == _CONTENT_TYPE]
        if len(content_types) != 1 or not _is_json_content_type(content_types[0]):
            await _json_error(
                scope, receive, send, 415, "Content-Type must be application/json"
            )
            return

        body_buffer = bytearray()
        while True:
            message = await receive()
            if message["type"] != "http.request":
                await _json_error(
                    scope, receive, send, 400, "Invalid request body"
                )
                return
            chunk = message.get("body", b"")
            if len(body_buffer) + len(chunk) > self.max_bytes:
                await _json_error(scope, receive, send, 413, "Request body too large")
                return
            body_buffer.extend(chunk)
            if not message.get("more_body", False):
                break

        if parsed_lengths and len(body_buffer) != parsed_lengths[0]:
            await _json_error(scope, receive, send, 400, "Invalid request body")
            return

        body = bytes(body_buffer)
        del body_buffer
        replayed = False

        async def replay_receive():
            nonlocal replayed
            if replayed:
                return {"type": "http.disconnect"}
            replayed = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay_receive, send)


class _PaperlessEvent(BaseModel):
    model_config = {"extra": "forbid"}

    document_id: StrictInt = Field(gt=0)
    event: Literal["document_added", "document_updated"]


def create_app(
    config: WorkerConfig,
    jobs: JobRepository,
    *,
    summary_field_readiness: Callable[[], bool] | None = None,
) -> FastAPI:
    app = FastAPI()
    metrics = WorkerMetrics()
    app.add_middleware(
        _WebhookRequestMiddleware,
        max_bytes=config.max_request_bytes,
        webhook_token_digest=sha256(
            config.webhook_token.get_secret_value().encode("utf-8")
        ).digest(),
    )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(
        _request: Request, _error: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            {"detail": "Invalid request"},
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    @app.post(
        _WEBHOOK_PATH,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def enqueue_event(event: _PaperlessEvent) -> dict[str, str]:
        try:
            jobs.enqueue(event.document_id, event.event)
        except Exception:
            metrics.record_enqueue("unavailable")
            logger.error(
                "job enqueue failed",
                extra={"document_id": event.document_id, "event": event.event},
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Service unavailable",
            ) from None
        metrics.record_enqueue("accepted")
        logger.info(
            "job enqueued",
            extra={"document_id": event.document_id, "event": event.event},
        )
        return {"status": "accepted"}

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/health/ready")
    def ready() -> JSONResponse:
        database_status = "ready"
        try:
            jobs.check_ready()
        except Exception:
            database_status = "not_ready"

        if summary_field_readiness is None:
            summary_status = "not_checked"
        else:
            try:
                summary_status = (
                    "ready" if summary_field_readiness() is True else "not_ready"
                )
            except Exception:
                summary_status = "not_ready"

        is_ready = database_status == summary_status == "ready"
        return JSONResponse(
            {
                "status": "ready" if is_ready else "not_ready",
                "components": {
                    "database": database_status,
                    "summary_field": summary_status,
                },
            },
            status_code=200 if is_ready else 503,
        )

    @app.get("/metrics")
    def prometheus_metrics() -> Response:
        try:
            snapshot = jobs.metrics_snapshot()
            content, content_type = metrics.render(snapshot)
        except Exception:
            logger.error("metrics snapshot failed")
            return JSONResponse(
                {"detail": "Service unavailable"},
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(content=content, media_type=content_type)

    return app


def _tokens_match(received: bytes, expected: bytes) -> bool:
    return secrets.compare_digest(received, expected)


def _valid_credential(credentials: list[bytes], expected_digest: bytes) -> bool:
    prefix = b"Bearer "
    syntax_valid = len(credentials) == 1 and credentials[0].startswith(prefix)
    token = credentials[0][len(prefix) :] if syntax_valid else b""
    syntax_valid = syntax_valid and is_valid_credential(token)
    received_digest = sha256(token if syntax_valid else b"").digest()
    matches = _tokens_match(received_digest, expected_digest)
    return syntax_valid and matches


def _parse_content_length(value: bytes) -> int:
    if not value or not value.isdigit():
        raise ValueError
    return int(value)


def _is_json_content_type(value: bytes) -> bool:
    media_type = value.split(b";", 1)[0].strip().lower()
    return media_type == b"application/json"


async def _json_error(
    scope: Scope,
    receive: Receive,
    send: Send,
    status_code: int,
    detail: str,
) -> None:
    response = JSONResponse({"detail": detail}, status_code=status_code)
    await response(scope, receive, send)
