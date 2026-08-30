"""asa-slack entry point.

``asa-slack`` (console script) or ``python -m asa_slack``.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from asa_slack import bot
from asa_slack.config import Config
from precis.utils.utc_logging import force_utc_timestamps

log = logging.getLogger(__name__)


def main() -> None:
    force_utc_timestamps()
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
    Delegates to :func:`precis.utils.db_log_handler.attach`."""
    from precis.utils.db_log_handler import attach

    attach(dsn, process="asa-slack", require_dsn=True)


if __name__ == "__main__":
    main()
