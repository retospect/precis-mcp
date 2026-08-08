"""Pin stdlib logging timestamps to UTC.

The fleet's hosts are pinned to UTC at the system level
(deploy/playbooks/00a-timezone.yml) and every deploy-rendered daemon
env carries ``TZ=UTC``, but ``%(asctime)s`` renders through
``time.localtime`` — so a process on a host whose zone drifted (or a
dev laptop) would still write local-time logs. Setting the class-level
converter makes every ``logging.Formatter`` — ours and any library's —
render UTC regardless of host state. Call it from each console-script
entry point before (or after — it's retroactive) ``basicConfig``.
"""

from __future__ import annotations

import logging
import time


def force_utc_timestamps() -> None:
    """Make every ``logging.Formatter`` render ``%(asctime)s`` in UTC.

    Class-level assignment on purpose: it also covers formatters built
    elsewhere (uvicorn, the worker's BufferedDBLogHandler) and ones
    created before the call. ``time.gmtime`` is a builtin, so it does
    not bind as a method. Idempotent.
    """
    logging.Formatter.converter = time.gmtime
