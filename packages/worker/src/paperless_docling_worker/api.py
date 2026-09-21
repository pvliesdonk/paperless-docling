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

from ._webhook import WEBHOOK_PATH as _WEBHOOK_PATH
from ._webhook import (
    _credential_is_valid,
    _is_json_content_type,
    _parse_content_length,
    _WebhookRequestMiddleware,
)
from .config import WorkerConfig
from .jobs import JobRepository
from .metrics import WorkerMetrics

logger = logging.getLogger(__name__)

__all__ = [
    "create_app",
    "_WebhookRequestMiddleware",
    "_is_json_content_type",
    "_parse_content_length",
]


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
        credential_validator=_valid_credential,
    )
    _register_invalid_request_handler(app)
    _register_enqueue_endpoint(app, jobs, metrics)
    _register_health_endpoints(app, jobs, summary_field_readiness)
    _register_metrics_endpoint(app, jobs, metrics)
    return app


def _register_invalid_request_handler(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def invalid_request(
        _request: Request, _error: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            {"detail": "Invalid request"},
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )


def _register_enqueue_endpoint(
    app: FastAPI, jobs: JobRepository, metrics: WorkerMetrics
) -> None:
    @app.post(_WEBHOOK_PATH, status_code=status.HTTP_202_ACCEPTED)
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


def _register_health_endpoints(
    app: FastAPI,
    jobs: JobRepository,
    summary_field_readiness: Callable[[], bool] | None,
) -> None:
    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/health/ready")
    def ready() -> JSONResponse:
        database_status = _database_readiness(jobs)
        summary_status = _summary_readiness(summary_field_readiness)
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


def _database_readiness(jobs: JobRepository) -> str:
    try:
        jobs.check_ready()
    except Exception:
        return "not_ready"
    return "ready"


def _summary_readiness(readiness: Callable[[], bool] | None) -> str:
    if readiness is None:
        return "not_checked"
    try:
        return "ready" if readiness() is True else "not_ready"
    except Exception:
        return "not_ready"


def _register_metrics_endpoint(
    app: FastAPI, jobs: JobRepository, metrics: WorkerMetrics
) -> None:
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


def _tokens_match(received: bytes, expected: bytes) -> bool:
    return secrets.compare_digest(received, expected)


def _valid_credential(credentials: list[bytes], expected_digest: bytes) -> bool:
    return _credential_is_valid(credentials, expected_digest, _tokens_match)
