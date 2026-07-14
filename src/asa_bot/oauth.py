"""Bootstrap the long-lived Claude Code OAuth token into a subprocess env.

asa_bot runs as a launchd daemon (``com.asa.bot``, user ``hermes``) and
spawns a fresh ``claude -p`` per Discord turn. launchd runs no shell hook,
so that subprocess never sees the ``CLAUDE_CODE_OAUTH_TOKEN`` an interactive
shell would export from ``~/.claude_oauth_token``. Without it, ``claude -p``
falls back to the interactive keychain credentials
(``~/.claude/.credentials.json``) — which are short-lived and lapse in about
a day, at which point every turn fails with ``Not logged in`` and asa replies
"Failed to authenticate." (the 2026-07-13 incident).

This mirrors precis's ``utils/claude_oauth.ensure_oauth_token`` — asa can't
import precis (separate venv), so the tiny helper is duplicated here. Any code
that shells out to ``claude -p`` from this daemon MUST run
:func:`ensure_oauth_token` on the subprocess env it passes.

Idempotent and override-safe: a token already present in the env (an
interactive shell, a launchd/plist var, an explicit test override) wins — we
only fill the gap, and only from the run-as user's home.
"""

from __future__ import annotations

import logging
from collections.abc import MutableMapping
from pathlib import Path

log = logging.getLogger(__name__)

#: Env var ``claude`` reads for non-interactive OAuth auth.
ENV_VAR = "CLAUDE_CODE_OAUTH_TOKEN"

#: File under ``$HOME`` holding the long-lived token (one line).
TOKEN_FILENAME = ".claude_oauth_token"


def ensure_oauth_token(env: MutableMapping[str, str]) -> None:
    """Fill :data:`ENV_VAR` in ``env`` from ``~/.claude_oauth_token``.

    Mutates ``env`` in place. No-op when the var is already set to a
    non-empty value (env override wins) or the file is missing / empty —
    in those cases ``claude`` keeps its existing resolution order.
    """
    if env.get(ENV_VAR):
        return
    token_path = Path.home() / TOKEN_FILENAME
    try:
        token = token_path.read_text().strip()
    except OSError:
        return
    if token:
        env[ENV_VAR] = token
        log.debug("oauth: loaded CLAUDE_CODE_OAUTH_TOKEN from %s", token_path)


__all__ = ["ENV_VAR", "TOKEN_FILENAME", "ensure_oauth_token"]
