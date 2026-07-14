"""asa-bot entry point.

``asa-bot`` (console script) or ``python -m asa_bot``.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from asa_bot import bot
from asa_bot.config import Config

log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        cfg = Config.load()
    except Exception:
        log.exception("config load failed")
        sys.exit(1)

    # Slice-5 centralised logs: attach the buffered DB log handler so
    # asa-bot's lines land in the same ``worker_logs`` table the
    # precis workers write to. ``precis logs --process asa-bot``
    # filters across the fleet without ssh-and-grep. The file
    # handler (basicConfig above → stdout/stderr → journald or
    # ``/var/log/asa-bot.log``) stays as the bootstrap + fallback
    # channel; DBLogHandler degrades to it on flush failure.
    _attach_db_log_handler(cfg.precis.database_url)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    stop_event = asyncio.Event()

    def _handle_signal(_signum: int, _frame: object | None) -> None:
        log.info("signal received — shutting down")
        loop.call_soon_threadsafe(stop_event.set)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        loop.run_until_complete(bot.run(cfg))
    finally:
        loop.close()


def _attach_db_log_handler(dsn: str) -> None:
    """Attach precis-mcp's BufferedDBLogHandler to asa-bot's logging.

    Best-effort: a failing handler attach (missing migration 0015,
    bad DSN, network) shouldn't kill the bot. The file handler
    keeps catching everything regardless.

    ``PRECIS_PROCESS=asa-bot`` should be set in the LaunchDaemon
    plist's EnvironmentVariables so every row carries the right
    process tag; if it isn't, we set it here as a fallback so the
    operator sees ``asa-bot`` in ``precis logs --process`` instead
    of NULL.
    """
    import os

    try:
        from precis.utils.db_log_handler import BufferedDBLogHandler

        os.environ.setdefault("PRECIS_PROCESS", "asa-bot")
        root = logging.getLogger()
        for existing in list(root.handlers):
            if isinstance(existing, BufferedDBLogHandler):
                return
        if not dsn:
            log.info("PRECIS_DATABASE_URL unset; skipping DB log handler")
            return
        handler = BufferedDBLogHandler(dsn)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        root.addHandler(handler)
        root.setLevel(logging.INFO)
    except Exception:
        logging.getLogger(__name__).exception(
            "failed to attach BufferedDBLogHandler — continuing without DB logs"
        )


if __name__ == "__main__":
    main()
