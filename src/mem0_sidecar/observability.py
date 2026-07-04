from __future__ import annotations

import json
import logging
import time
from contextvars import ContextVar
from typing import Any
from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from mem0_sidecar.config import SidecarSettings

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
HTTP_LOGGER = logging.getLogger("mem0_sidecar.http")


def get_request_id() -> str | None:
    return _request_id.get()


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "time": self.formatTime(record, self.datefmt),
        }
        for key in (
            "request_id",
            "method",
            "path",
            "status_code",
            "duration_ms",
            "client_host",
            "upstream_base_url",
            "error_type",
        ):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, sort_keys=True)


def configure_logging(settings: SidecarSettings) -> None:
    level = logging.getLevelName(settings.log_level.upper())
    if not isinstance(level, int):
        level = logging.INFO
    logging.getLogger("mem0_sidecar").setLevel(level)

    if settings.log_format != "json":
        return

    formatter = JsonLogFormatter()
    root_logger = logging.getLogger()
    for handler in root_logger.handlers:
        handler.setFormatter(formatter)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, *, request_id_header: str) -> None:
        super().__init__(app)
        self.request_id_header = request_id_header

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get(self.request_id_header) or str(uuid4())
        token = _request_id.set(request_id)
        started_at = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers[self.request_id_header] = request_id
            return response
        except Exception:
            HTTP_LOGGER.exception(
                "http_request_failed",
                extra=self._log_extra(request, status_code, started_at),
            )
            raise
        finally:
            HTTP_LOGGER.info(
                "http_request_completed",
                extra=self._log_extra(request, status_code, started_at),
            )
            _request_id.reset(token)

    def _log_extra(
        self,
        request: Request,
        status_code: int,
        started_at: float,
    ) -> dict[str, Any]:
        client_host = request.client.host if request.client else None
        return {
            "request_id": get_request_id(),
            "method": request.method,
            "path": request.url.path,
            "status_code": status_code,
            "duration_ms": round((time.perf_counter() - started_at) * 1000, 3),
            "client_host": client_host,
        }
