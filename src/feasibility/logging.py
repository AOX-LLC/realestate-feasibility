"""Log setup. Secrets are redacted from every record before any handler formats it."""

import logging
import sys
from collections.abc import Iterable

from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError

REDACTED = "[REDACTED]"
NOISY_HTTP_LOGGERS = ("httpx", "httpcore")


def redact(text: str, secrets: Iterable[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    return text


def describe_error(error: BaseException) -> str:
    """An error as one line that can be kept (a run's error, a job's last error) without keeping
    data. A validation error is summarised without the values that failed, and a database error
    by its class, SQLSTATE and constraint, not its message: both can quote a row or a model's text.
    """
    if isinstance(error, ValidationError):
        problems = "; ".join(
            f"{'.'.join(str(part) for part in problem['loc'])}: {problem['msg']}"
            for problem in error.errors(include_input=False, include_url=False)
        )
        return f"ValidationError: {error.error_count()} problems in {error.title}: {problems}"
    if isinstance(error, DBAPIError):
        original = error.orig
        sqlstate = getattr(original, "sqlstate", None) or "unknown"
        constraint = getattr(getattr(original, "diag", None), "constraint_name", None)
        where = f", constraint {constraint}" if constraint else ""
        return f"{type(error).__name__}: database error (SQLSTATE {sqlstate}{where})"
    return f"{type(error).__name__}: {error}"


class SecretRedactingFilter(logging.Filter):
    """Replaces literal secret values in a record's message, arguments and traceback."""

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        self._secrets = [secret for secret in secrets if secret]

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        record.msg = redact(record.getMessage(), self._secrets)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text, self._secrets)
        if record.stack_info:
            record.stack_info = redact(record.stack_info, self._secrets)
        return True


def configure_logging(secrets: Iterable[str], level: int = logging.INFO) -> None:
    # Logger-level filters do not run for records propagated from child loggers,
    # so the redaction filter sits on the handler every record passes through.
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(SecretRedactingFilter(secrets))

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    # httpx logs full request lines at INFO; nothing below WARNING is needed from it.
    for name in NOISY_HTTP_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
