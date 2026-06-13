"""
shared/logging/__init__.py — Structured JSON logging for all services.

Every log line is emitted as a JSON object so Logtail can index and
query individual fields — workspace_id, trace_id, duration_ms, etc.

Usage:
  from shared.logging import get_logger, bind_context, clear_context

  logger = get_logger(__name__)
  logger.info("Question answered", extra={"confidence": 0.87})

Context binding (attach fields to every log line in a request):
  bind_context(trace_id="abc123", workspace_id=str(workspace_id))
  logger.info("Starting pipeline")   # trace_id and workspace_id auto-included
  clear_context()                    # called in middleware after response

In Logtail you can then query:
  workspace_id = "some-uuid"
  level = "ERROR"
  duration_ms > 3000
"""
from __future__ import annotations

import json
import logging
import sys
import traceback
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

from config import settings


# ── Per-request context (async-safe via ContextVar) ───────────────────────────

_log_context: ContextVar[dict[str, Any]] = ContextVar("log_context", default={})


def bind_context(**fields: Any) -> None:
  """
  Attach fields to every log line emitted during the current request/task.
  Safe across async tasks — each coroutine gets its own copy.

    bind_context(trace_id="abc123", workspace_id="uuid", platform="slack")
  """
  _log_context.set({**_log_context.get({}), **fields})


def clear_context() -> None:
  """Drop all context. Called from request middleware after response is sent."""
  _log_context.set({})


def get_context() -> dict[str, Any]:
  return _log_context.get({})


# ── JSON formatter ────────────────────────────────────────────────────────────

class JSONFormatter(logging.Formatter):
  """
  Emits every log record as a single-line JSON object.

  Always present:
    timestamp   ISO-8601 UTC
    level       DEBUG / INFO / WARNING / ERROR / CRITICAL
    logger      module path e.g. "services.qa_engine.service"
    message     the log message
    service     "Vidhya"
    env         development / staging / production

  Added from bind_context() when present:
    trace_id, workspace_id, platform, user_id, duration_ms, ...

  Added when an exception is logged:
    exc_type, exc_message, exc_traceback
  """

  def format(self, record: logging.LogRecord) -> str:
    payload: dict[str, Any] = {
      "timestamp": datetime.now(timezone.utc).isoformat(),
      "level":     record.levelname,
      "logger":    record.name,
      "message":   record.getMessage(),
      "service":   "Vidhya",
      "env":       settings.APP_ENV,
    }

    # Merge per-request context fields
    payload.update(get_context())

    # Extra fields passed via extra={} kwarg to logger call
    for key, value in record.__dict__.items():
      if key not in _STDLIB_ATTRS and not key.startswith("_"):
        payload[key] = value

    # Exception details
    if record.exc_info and record.exc_info[0] is not None:
      exc_type, exc_value, exc_tb = record.exc_info
      payload["exc_type"] = exc_type.__name__
      payload["exc_message"] = str(exc_value)
      payload["exc_traceback"] = "".join(traceback.format_tb(exc_tb)).strip()

    return json.dumps(payload, default=str)


_STDLIB_ATTRS = frozenset({
  "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
  "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
  "created", "msecs", "relativeCreated", "thread", "threadName",
  "processName", "process", "message", "taskName",
})


# ── Logtail handler ───────────────────────────────────────────────────────────

def _build_logtail_handler(token: str) -> logging.Handler | None:
  """
  Returns a Logtail handler that ships logs in the background.
  Returns None if the logtail-python package is not installed.
  """
  try:
    from logtail import LogtailHandler
    handler = LogtailHandler(source_token=token)
    handler.setLevel(logging.INFO)   # never ship DEBUG to Logtail
    return handler
  except ImportError:
    logging.getLogger(__name__).warning(
      "logtail-python not installed — Logtail shipping disabled. "
      "pip install logtail-python"
    )
    return None


# ── Setup ─────────────────────────────────────────────────────────────────────

def configure_logging() -> None:
  """
  Configure structured logging for the whole application.
  Call ONCE at startup in app.py lifespan, before anything else logs.

  Development  →  plain text to stdout (readable in terminal)
  Staging/Prod →  JSON to stdout (captured by systemd journal) + Logtail forwarding if LOGTAIL_TOKEN is set
  """
  root = logging.getLogger()
  root.setLevel(getattr(logging, settings.LOG_LEVEL))
  root.handlers.clear()

  if settings.IS_DEVELOPMENT:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
      fmt="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
      datefmt="%H:%M:%S",
    ))
    root.addHandler(handler)
  else:
    # JSON → stdout → systemd journal
    json_handler = logging.StreamHandler(sys.stdout)
    json_handler.setFormatter(JSONFormatter())
    root.addHandler(json_handler)

    # Logtail (optional)

  # Silence noisy libraries
  logging.getLogger("sqlalchemy.engine").setLevel(
    logging.DEBUG if settings.IS_DEVELOPMENT else logging.WARNING
  )
  logging.getLogger("httpx").setLevel(logging.WARNING)
  logging.getLogger("httpcore").setLevel(logging.WARNING)
  logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
  logging.getLogger("celery").setLevel(logging.INFO)


# ── Public API ────────────────────────────────────────────────────────────────

def get_logger(name: str) -> logging.Logger:
  """
  Get a named logger. Always pass __name__:

    from shared.logging import get_logger
    logger = get_logger(__name__)
    logger.info("answer ready", extra={"confidence": 0.87, "duration_ms": 1240})
    logger.error("pipeline failed", exc_info=True)
  """
  return logging.getLogger(name)


# ── Convenience wrapper ───────────────────────────────────────────────────────

class BoundLogger:
  """
  Thin wrapper around stdlib Logger that accepts keyword args directly
  instead of requiring extra={} every time.

    logger = get_logger(__name__)
    logger.info("Question answered", confidence=0.87, duration_ms=1240)

  is equivalent to:

    logging.getLogger(__name__).info(
        "Question answered",
        extra={"confidence": 0.87, "duration_ms": 1240}
    )
  """

  def __init__(self, name: str) -> None:
    self._logger = logging.getLogger(name)

  def _log(self, level: int, msg: str, **fields: Any) -> None:
    if self._logger.isEnabledFor(level):
      self._logger.log(level, msg, extra=fields if fields else None)

  def debug(self, msg: str, **fields: Any) -> None:
    self._log(logging.DEBUG, msg, **fields)

  def info(self, msg: str, **fields: Any) -> None:
    self._log(logging.INFO, msg, **fields)

  def warning(self, msg: str, **fields: Any) -> None:
    self._log(logging.WARNING, msg, **fields)

  def error(self, msg: str, exc_info: bool = False, **fields: Any) -> None:
    if self._logger.isEnabledFor(logging.ERROR):
      self._logger.log(logging.ERROR, msg, exc_info=exc_info, extra=fields if fields else None)

  def critical(self, msg: str, exc_info: bool = False, **fields: Any) -> None:
    if self._logger.isEnabledFor(logging.CRITICAL):
      self._logger.log(logging.CRITICAL, msg, exc_info=exc_info, extra=fields if fields else None)


def get_logger(name: str) -> BoundLogger:  # type: ignore[override]
  """
  Returns a BoundLogger that accepts keyword args directly:

    logger = get_logger(__name__)
    logger.info("Started", env="production", workers=2)
    logger.error("Pipeline failed", exc_info=True, workspace_id=wid)
  """
  return BoundLogger(name)
