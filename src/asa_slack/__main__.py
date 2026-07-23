"""asa-slack entry point.

``asa-slack`` (console script) or ``python -m asa_slack``.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from asa_slack import bot
from asa_slack.config import Config

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

    _attach_db_log_handler(cfg.precis.database_url)

    asyncio.run(bot.run(cfg))


def _attach_db_log_handler(dsn: str) -> None:
    """Attach precis-mcp's BufferedDBLogHandler — mirrors asa_bot's, tagged
    ``asa-slack`` so ``precis logs --process asa-slack`` filters cleanly.
    Best-effort: a failing attach shouldn't kill the bot."""
    import os

    try:
        from precis.utils.db_log_handler import BufferedDBLogHandler

        os.environ.setdefault("PRECIS_PROCESS", "asa-slack")
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
