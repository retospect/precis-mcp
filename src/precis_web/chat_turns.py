"""Ask chat page: what a turn needs on the web host (gr681394).

:func:`preflight` names each missing piece as one readable line; the web
lifespan logs them once at startup. The runner lands in a later slice.
"""

from __future__ import annotations

import logging
import os
import shutil

log = logging.getLogger(__name__)

#: Env var naming the rendered MCP config the chat's ``claude -p`` loads.
MCP_CONFIG_ENV = "PRECIS_CHAT_MCP_CONFIG"


def preflight() -> list[str]:
    """One line per missing piece of a chat turn; empty when all present."""
    from precis.secrets import get_secret

    problems: list[str] = []
    if shutil.which("claude") is None:
        problems.append("the `claude` binary is not on PATH")
    cfg = os.environ.get(MCP_CONFIG_ENV, "")
    if not cfg:
        problems.append(f"{MCP_CONFIG_ENV} is not set (no chat MCP config deployed)")
    elif not os.path.isfile(cfg):
        problems.append(f"{MCP_CONFIG_ENV} names {cfg}, which does not exist")
    try:
        api_key = get_secret("ANTHROPIC_API_KEY")
    except UnicodeDecodeError:
        # A malformed fallback secret file must not prevent web startup.
        api_key = None
    if not api_key:
        problems.append(
            "ANTHROPIC_API_KEY is not resolvable; store it on the /secrets page"
        )
    return problems


def log_preflight() -> None:
    """Log each :func:`preflight` line (warning), or one info line if clean."""
    lines = preflight()
    for line in lines:
        log.warning("ask chat preflight: %s", line)
    if not lines:
        log.info("ask chat preflight: ok")
