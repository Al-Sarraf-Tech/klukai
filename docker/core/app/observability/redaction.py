"""Keep query credentials out of logs and exported request attributes."""

from __future__ import annotations

import logging
import re
from urllib.parse import unquote

_QUERY_VALUE = re.compile(r"([^?&=\s\"'<>]+)=([^?&\s\"'<>]*)")
_BEARER = re.compile(r"(?i)(\bBearer\s+)[\w.~+/=-]+")
_SECRET_KEYS = {"token", "access_token", "api_key"}


def redact_credentials(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        if unquote(match[1]).lower() in _SECRET_KEYS:
            return f"{match[1]}=***"
        return match[0]

    return _BEARER.sub(r"\1***", _QUERY_VALUE.sub(replace, text))


class CredentialLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                redact_credentials(v) if isinstance(v, str) else v for v in record.args
            )
        elif isinstance(record.args, dict):
            record.args = {
                k: redact_credentials(v) if isinstance(v, str) else v
                for k, v in record.args.items()
            }
        # Also cover preformatted messages and credentials split across a
        # format string and its arguments. Preserve typed arguments otherwise.
        message = record.getMessage()
        redacted = redact_credentials(message)
        if message != redacted:
            record.msg, record.args = redacted, ()
        if record.exc_info:
            # Do not modify the exception object used by application control
            # flow. Preserve its traceback as sanitized log text instead.
            record.exc_text = logging.Formatter().formatException(record.exc_info)
            record.exc_info = None
        if record.exc_text:
            record.exc_text = redact_credentials(record.exc_text)
        if record.stack_info:
            record.stack_info = redact_credentials(record.stack_info)
        return True


def install_log_redaction() -> None:
    # Logger filters do not run on descendant records during propagation.
    # Cover Uvicorn's separate handshake/error/access loggers directly, and
    # existing root handlers for application/exporter messages.
    targets: list[logging.Filterer] = [
        logging.getLogger(name)
        for name in (
            "uvicorn",
            "uvicorn.error",
            "uvicorn.access",
            "uvicorn.asgi",
            "httpx",
            "httpcore",
        )
    ]
    targets.extend(logging.getLogger().handlers)
    for target in targets:
        if not any(isinstance(f, CredentialLogFilter) for f in target.filters):
            target.addFilter(CredentialLogFilter())
