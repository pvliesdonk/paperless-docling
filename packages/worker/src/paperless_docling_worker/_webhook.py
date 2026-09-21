from __future__ import annotations

import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from hashlib import sha256

from fastapi import status
from fastapi.responses import JSONResponse
from starlette.types import Message, Receive, Scope, Send

from .credentials import is_valid_credential

WEBHOOK_PATH = "/v1/events/paperless"

_AUTHORIZATION = b"authorization"
_CONTENT_LENGTH = b"content-length"
_CONTENT_TYPE = b"content-type"

CredentialValidator = Callable[[list[bytes], bytes], bool]
DigestComparator = Callable[[bytes, bytes], bool]


def _default_credential_validator(
    credentials: list[bytes], expected_digest: bytes
) -> bool:
    return _credential_is_valid(credentials, expected_digest, secrets.compare_digest)


@dataclass(frozen=True)
class _RequestError(Exception):
    status_code: int
    detail: str
    headers: dict[str, str] | None = None


class _WebhookRequestMiddleware:
    def __init__(
        self,
        app,
        *,
        max_bytes: int,
        webhook_token_digest: bytes,
        credential_validator: CredentialValidator = _default_credential_validator,
    ) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.webhook_token_digest = webhook_token_digest
        self.credential_validator = credential_validator

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not _targets_webhook(scope):
            await self.app(scope, receive, send)
            return
        try:
            expected_length = _validate_headers(
                scope.get("headers", ()),
                self.max_bytes,
                self.webhook_token_digest,
                self.credential_validator,
            )
            body = await _read_capped_body(receive, self.max_bytes)
            _validate_body_length(body, expected_length)
        except _RequestError as error:
            await _send_error(scope, receive, send, error)
            return
        await self.app(scope, _replay_body(body), send)


def _targets_webhook(scope: Scope) -> bool:
    return (
        scope["type"] == "http"
        and scope.get("method") == "POST"
        and scope.get("path") == WEBHOOK_PATH
    )


def _validate_headers(
    headers: Sequence[tuple[bytes, bytes]],
    max_bytes: int,
    expected_digest: bytes,
    credential_validator: CredentialValidator,
) -> int | None:
    credentials = [value for name, value in headers if name == _AUTHORIZATION]
    if not credential_validator(credentials, expected_digest):
        raise _RequestError(
            status.HTTP_401_UNAUTHORIZED,
            "Unauthorized",
            {"WWW-Authenticate": "Bearer"},
        )
    expected_length = _validate_content_length(headers, max_bytes)
    content_types = [value for name, value in headers if name == _CONTENT_TYPE]
    if len(content_types) != 1 or not _is_json_content_type(content_types[0]):
        raise _RequestError(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "Content-Type must be application/json",
        )
    return expected_length


def _validate_content_length(
    headers: Sequence[tuple[bytes, bytes]], max_bytes: int
) -> int | None:
    lengths = [value for name, value in headers if name == _CONTENT_LENGTH]
    try:
        parsed_lengths = [_parse_content_length(value) for value in lengths]
    except ValueError:
        raise _RequestError(400, "Invalid Content-Length") from None
    if parsed_lengths and len(set(parsed_lengths)) != 1:
        raise _RequestError(400, "Invalid Content-Length")
    if parsed_lengths and parsed_lengths[0] > max_bytes:
        raise _RequestError(413, "Request body too large")
    return parsed_lengths[0] if parsed_lengths else None


async def _read_capped_body(receive: Receive, max_bytes: int) -> bytes:
    body_buffer = bytearray()
    while True:
        message = await receive()
        if message["type"] != "http.request":
            raise _RequestError(400, "Invalid request body")
        chunk = message.get("body", b"")
        if len(body_buffer) + len(chunk) > max_bytes:
            raise _RequestError(413, "Request body too large")
        body_buffer.extend(chunk)
        if not message.get("more_body", False):
            break
    body = bytes(body_buffer)
    del body_buffer
    return body


def _validate_body_length(body: bytes, expected_length: int | None) -> None:
    if expected_length is not None and len(body) != expected_length:
        raise _RequestError(400, "Invalid request body")


def _replay_body(body: bytes) -> Receive:
    replayed = False

    async def replay_receive() -> Message:
        nonlocal replayed
        if replayed:
            return {"type": "http.disconnect"}
        replayed = True
        return {"type": "http.request", "body": body, "more_body": False}

    return replay_receive


async def _send_error(
    scope: Scope,
    receive: Receive,
    send: Send,
    error: _RequestError,
) -> None:
    response = JSONResponse(
        {"detail": error.detail},
        status_code=error.status_code,
        headers=error.headers,
    )
    await response(scope, receive, send)


def _credential_is_valid(
    credentials: list[bytes],
    expected_digest: bytes,
    compare_digest: DigestComparator,
) -> bool:
    prefix = b"Bearer "
    syntax_valid = len(credentials) == 1 and credentials[0].startswith(prefix)
    token = credentials[0][len(prefix) :] if syntax_valid else b""
    syntax_valid = syntax_valid and is_valid_credential(token)
    received_digest = sha256(token if syntax_valid else b"").digest()
    matches = compare_digest(received_digest, expected_digest)
    return syntax_valid and matches


def _parse_content_length(value: bytes) -> int:
    if not value or not value.isdigit():
        raise ValueError
    return int(value)


def _is_json_content_type(value: bytes) -> bool:
    media_type = value.split(b";", 1)[0].strip().lower()
    return media_type == b"application/json"
