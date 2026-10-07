"""Redact supplier API query keys in HTTPX's informational request log.

Farnell and Mouser require query credentials; HTTPX logs the request URL at
INFO even on success. Keep an idempotent filter on that logger so concurrent
requests cannot expose keys when logging is enabled. Actual requests are
unchanged; errors returned to callers use only safe stage/status diagnostics.
"""

from __future__ import annotations

import logging

import httpx


class _QueryKeys(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            args = []
            for arg in record.args:
                if isinstance(arg, httpx.URL):
                    for name in arg.params:
                        if name.lower() in {"apikey", "callinfo.apikey"}:
                            arg = arg.copy_set_param(name, "REDACTED")
                args.append(arg)
            record.args = tuple(args)
        return True


def protect_request_logs() -> None:
    logger = logging.getLogger("httpx")
    if not any(isinstance(item, _QueryKeys) for item in logger.filters):
        logger.addFilter(_QueryKeys())
