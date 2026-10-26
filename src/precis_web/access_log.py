"""Redact web bearer query tokens before access records reach any handler."""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import unquote_plus

_QUERY_VALUE = re.compile(r'([?&])([^?&=\s"#]+)=([^&\s"#]*)')


def _redact(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        if unquote_plus(match[2]) == "t":
            return f"{match[1]}{match[2]}=REDACTED"
        return match[0]

    return _QUERY_VALUE.sub(replace, text)


def _redact_target(target: str) -> str:
    path, separator, query = target.partition("?")
    if not separator:
        return target
    parts = query.split("&")
    for index, part in enumerate(parts):
        key, equals, _ = part.partition("=")
        if equals and unquote_plus(key) == "t":
            parts[index] = f"{key}=REDACTED"
    return path + separator + "&".join(parts)


class AccessTokenFilter(logging.Filter):
    """Mask every ``t`` query value, retaining Uvicorn's formatter args."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != "uvicorn.access":
            return True
        if isinstance(record.args, tuple) and len(record.args) == 5:
            client, method, target, version, status = record.args
            if isinstance(target, str):
                record.args = (client, method, _redact_target(target), version, status)
        else:
            # Also accept access records whose request line is already rendered.
            text = record.getMessage()
            masked = _redact(text)
            if masked != text:
                record.msg = masked
                record.args = ()
        return True


_FILTER = AccessTokenFilter()
_factory: Any = None


def install_access_token_redaction() -> None:
    """Chain the active record factory; Uvicorn dictConfig cannot remove it."""
    global _factory
    old = logging.getLogRecordFactory()
    if old is _factory:
        return

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = old(*args, **kwargs)
        _FILTER.filter(record)
        return record

    _factory = factory
    logging.setLogRecordFactory(factory)
