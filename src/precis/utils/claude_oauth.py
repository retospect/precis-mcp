"""Bootstrap the long-lived Claude Code OAuth token into a subprocess env.

launchd-spawned daemons (``com.precis.worker-agent``, ``com.precis.dream``)
don't run any shell hook, so a ``claude -p`` subprocess they spawn never sees
the ``CLAUDE_CODE_OAUTH_TOKEN`` that an interactive shell would export from
``~/.claude_oauth_token`` (see the note in ``utils/claude_agent``). Without it,
``claude -p`` falls back to the (possibly stale / revoked) keychain
credentials and fails with a ``401 Invalid authentication credentials``.

This is the 2026-07-12 incident: ``claude_agent`` bootstrapped the token from
the file, but ``plan_tick`` and ``claude_quota`` each spawned ``claude -p``
with a raw ``dict(os.environ)`` and so authenticated off the stale keychain —
every planner tick and quota refresh 401'd once dispatch recovered. Any code
that shells out to ``claude -p`` from a daemon MUST run :func:`ensure_oauth_token`
on the subprocess env it passes.

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
        token = ""
    if not token:
        # Vault fallback (secrets vault, ADR 0055): when the file is absent, a
        # process that has bound a store (server / worker) can source the token
        # from the DB. get_secret is env→vault→file, best-effort (never raises).
        try:
            from precis import secrets as _secrets

            token = _secrets.get_secret(ENV_VAR) or ""
        except Exception:
            token = ""
    if token:
        env[ENV_VAR] = token
        log.debug("claude_oauth: loaded %s (file or vault)", ENV_VAR)


__all__ = ["ENV_VAR", "TOKEN_FILENAME", "ensure_oauth_token"]
