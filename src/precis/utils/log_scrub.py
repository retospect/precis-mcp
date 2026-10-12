"""Mask credential-shaped text in log records.

Logs are mirrored to journald and (for workers) to the ``logs`` table, so a
credential that reaches a log line spreads like one pasted into memory.
:class:`SecretScrubFilter` rewrites a record's message -- after ``%``-formatting
the args -- with :func:`precis.utils.secret_scan.mask_secrets`. Ordinary lines
skip the full scan via the cheap :func:`~precis.utils.secret_scan.might_contain_secret`
pre-check (one combined regex).

:func:`install_log_scrub` attaches the filter at *record creation* (the
``LogRecordFactory``), not to a handler: a handler-level filter would miss
handlers added later (the worker's DB handler) and ones libraries build
(uvicorn). Call it next to ``force_utc_timestamps()`` in each entry point.

Recursion: the scan never logs (``warn=False``), records from
``precis.utils.secret_scan`` itself are passed through untouched, and a
re-entrancy guard covers anything else that logs while scrubbing.
Tracebacks (``exc_info``) are not rewritten.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from precis.utils.secret_scan import might_contain_secret

_SELF = "precis.utils.secret_scan"
_guard = threading.local()


class SecretScrubFilter(logging.Filter):
    """Always passes the record; masks credential shapes in its message."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == _SELF or getattr(_guard, "busy", False):
            return True
        _guard.busy = True
        try:
            msg = record.msg
            if record.args:
                # Cheap pre-check on the template and string args first, so a
                # line with nothing suspicious is not formatted here at all.
                parts = [msg, *record.args] if isinstance(record.args, tuple) else [msg]
                if not any(
                    isinstance(p, str) and might_contain_secret(p) for p in parts
                ) and not _has_nonstring(record.args):
                    return True
                text = record.getMessage()
            elif isinstance(msg, str):
                text = msg
            else:
                text = str(msg)
            if not might_contain_secret(text):
                return True
            from precis.utils.secret_scan import mask_secrets

            masked = mask_secrets(text, warn=False)
            if masked != text:
                if not _mask_args_in_place(record, text):
                    record.msg = masked
                    record.args = None
        except Exception:  # a log filter must never break logging
            return True
        finally:
            _guard.busy = False
        return True


def _mask_args_in_place(record: logging.LogRecord, text: str) -> bool:
    """Mask string args element-wise, keeping ``record.args`` a tuple.

    Formatters that unpack ``record.args`` (uvicorn's ``AccessFormatter``
    reads a 5-tuple) crash on ``args=None``, so collapsing to a pre-formatted
    message is the last resort. True when the rewritten record formats clean.
    """
    from precis.utils.secret_scan import mask_secrets

    args = record.args
    if not isinstance(args, tuple):
        return False
    new_args = tuple(
        mask_secrets(a, warn=False) if isinstance(a, str) else a for a in args
    )
    try:
        candidate = record.msg % new_args if isinstance(record.msg, str) else None
    except Exception:
        return False
    if candidate is None or mask_secrets(candidate, warn=False) != candidate:
        return False
    record.args = new_args
    return True


def _has_nonstring(args: Any) -> bool:
    """Args whose ``str()`` could carry a secret (exceptions, dicts, ...)."""
    if isinstance(args, tuple):
        return any(
            not isinstance(a, int | float | bool | type(None) | str) for a in args
        )
    return True  # mapping args


_FILTER = SecretScrubFilter()
_installed = False


def install_log_scrub() -> None:
    """Scrub every log record process-wide, at creation. Idempotent."""
    global _installed
    if _installed:
        return
    _installed = True
    old = logging.getLogRecordFactory()

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = old(*args, **kwargs)
        _FILTER.filter(record)
        return record

    logging.setLogRecordFactory(factory)
