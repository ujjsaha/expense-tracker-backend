"""Request-correlated logging with secret redaction (SRS section 7).

Two guarantees:
  * every log line carries the request id, so a client report maps to server logs;
  * passwords, OTPs and tokens never reach the log output, whatever a caller does.
"""

import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

REQUEST_ID_HEADER = "X-Request-ID"

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

REDACTED = "***REDACTED***"

# Longest keys first so `confirm_password` is not shadowed by `password`.
_SENSITIVE_KEYS = (
    "confirm_password",
    "new_password",
    "refresh_token",
    "access_token",
    "jwt_secret_key",
    "authorization",
    "password",
    "secret",
    "token",
    "otp",
)

# Matches `key: value`, `key=value`, `"key": "value"` in whatever a caller
# interpolated into the message.
_KV_PATTERN = re.compile(
    r"(?i)([\"']?\b(?:" + "|".join(_SENSITIVE_KEYS) + r")\b[\"']?\s*[:=]\s*)([\"']?)([^\"',}\s]+)"
)

_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]+")


def redact(text: str) -> str:
    """Strip secret values out of an already-formatted log message."""
    text = _KV_PATTERN.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}{m.group(2)}", text)
    return _BEARER_PATTERN.sub(f"Bearer {REDACTED}", text)


class RedactionFilter(logging.Filter):
    """Rewrites the record in place so no handler can emit the original."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - a broken record must not kill the request
            return True

        cleaned = redact(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = ()
        return True


class CorrelationIdFilter(logging.Filter):
    """Injects `request_id` so the formatter can reference it unconditionally."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Honours an inbound X-Request-ID, otherwise mints one, and echoes it back."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        token = request_id_var.set(request_id)
        request.state.request_id = request_id
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response


def setup_logging(level: str = "INFO") -> None:
    """Idempotent root-logger configuration."""
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(request_id)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    handler.addFilter(CorrelationIdFilter())
    handler.addFilter(RedactionFilter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())

    # uvicorn installs its own handlers; route them through ours so access logs
    # are redacted and correlated too.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True
