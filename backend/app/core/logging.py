"""Structured JSON logging.

Design notes:
* One log line per event, JSON encoded, so that Loki/CloudWatch/Datadog can
  index fields without regex parsing.
* `request_id` is pulled from a contextvar, so any log call anywhere in the
  request lifecycle is automatically correlated without threading a logger
  through every function signature.
* Redaction is enforced at the boundary: `safe_url()` is the only sanctioned
  way to put a user-submitted URL into a log line, and it drops the query
  string and fragment because those routinely carry tokens and PII.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from app.core.config import Settings
from app.core.context import get_request_id, get_route

# Attributes that live on every LogRecord; anything else a caller attached via
# `logger.info(..., extra={...})` is treated as a structured field.
_RESERVED_RECORD_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)

# Field names that must never be emitted, even if a caller passes them.
_REDACTED_KEYS = frozenset(
    {
        "authorization",
        "cookie",
        "set-cookie",
        "password",
        "secret",
        "token",
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "database_url",
        "redis_url",
    }
)

_REDACTED = "[redacted]"


def safe_url(raw_url: str, *, max_path_length: int = 64) -> str:
    """Return a log-safe summary of a user-submitted URL.

    Keeps scheme + host + a truncated path. Deliberately drops the three parts
    that carry secrets:

    * **query string** -- password-reset tokens, session ids, email addresses.
    * **fragment** -- where the OAuth implicit flow puts access tokens.
    * **userinfo** -- `https://user:hunter2@host/`. Note this is built from
      `hostname`, not `netloc`: `netloc` *includes* userinfo, so using it
      would write the password straight into the log line. Such URLs are
      rejected by `validate_destination_url`, but this helper is documented
      as the sanctioned way to log any user-submitted URL and is called on
      paths that run before validation, so it must be safe on its own.
    """
    try:
        parts = urlsplit(raw_url)
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        return "[unparseable-url]"
    if not parts.scheme and not hostname:
        return "[unparseable-url]"

    host = hostname or ""
    if ":" in host:  # IPv6 literal, which `hostname` returns unbracketed
        host = f"[{host}]"
    if port is not None:
        host = f"{host}:{port}"

    path = parts.path or ""
    if len(path) > max_path_length:
        path = path[:max_path_length] + "..."
    suffix = "?[redacted]" if parts.query else ""
    return f"{parts.scheme}://{host}{path}{suffix}"


class JsonFormatter(logging.Formatter):
    """Formats records as single-line JSON objects."""

    def __init__(self, *, service: str, environment: str, instance_id: str) -> None:
        super().__init__()
        self._base: dict[str, Any] = {
            "service": service,
            "env": environment,
            "instance": instance_id,
        }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            **self._base,
        }

        request_id = get_request_id()
        if request_id:
            payload["request_id"] = request_id
        route = get_route()
        if route:
            payload.setdefault("route", route)

        for key, value in record.__dict__.items():
            if key in _RESERVED_RECORD_ATTRS or key.startswith("_"):
                continue
            payload[key] = _REDACTED if key.lower() in _REDACTED_KEYS else value

        if record.exc_info:
            exc_type = record.exc_info[0]
            payload["error"] = {
                "type": exc_type.__name__ if exc_type else "UnknownError",
                "message": str(record.exc_info[1]),
                "stack": self.formatException(record.exc_info),
            }
        elif record.exc_text:
            payload["error"] = {"stack": record.exc_text}

        return json.dumps(payload, default=str, separators=(",", ":"))


class ConsoleFormatter(logging.Formatter):
    """Human-friendly formatter for local development."""

    def format(self, record: logging.LogRecord) -> str:
        request_id = get_request_id()
        prefix = f"[{request_id[:8]}] " if request_id else ""
        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _RESERVED_RECORD_ATTRS and not key.startswith("_")
        }
        suffix = f" {extras}" if extras else ""
        base = f"{record.levelname:<8} {record.name} {prefix}{record.getMessage()}{suffix}"
        if record.exc_info:
            base = f"{base}\n{self.formatException(record.exc_info)}"
        return base


def configure_logging(settings: Settings) -> None:
    """Install the root log handler. Idempotent."""
    formatter: logging.Formatter
    if settings.log_format == "json":
        formatter = JsonFormatter(
            service=settings.app_name,
            environment=settings.app_env,
            instance_id=settings.instance_id,
        )
    else:
        formatter = ConsoleFormatter()

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(settings.log_level)

    # Uvicorn's own access log is replaced by our access-log middleware, which
    # knows the request id, the matched route template, and the duration.
    logging.getLogger("uvicorn.access").disabled = True
    for noisy in ("uvicorn", "uvicorn.error", "gunicorn.error", "gunicorn.access"):
        logger = logging.getLogger(noisy)
        logger.handlers = []
        logger.propagate = True

    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if settings.db_echo else logging.WARNING
    )


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
