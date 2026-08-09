"""Bootstrap the long-lived Claude Code OAuth token into a subprocess env.

asa_bot runs as a launchd daemon (typically under a dedicated service
user) and spawns a fresh ``claude -p`` per Discord turn. launchd runs no shell hook,
so that subprocess never sees the ``CLAUDE_CODE_OAUTH_TOKEN`` an interactive
shell would export from ``~/.claude_oauth_token``. Without it, ``claude -p``
falls back to the interactive keychain credentials
(``~/.claude/.credentials.json``) — which are short-lived and lapse in about
a day, at which point every turn fails with ``Not logged in`` and asa replies
"Failed to authenticate." (the 2026-07-13 incident).

This mirrors precis's ``utils/claude_oauth.ensure_oauth_token``; the tiny
helper is duplicated here (asa keeps its own minimal surface). Any code that
shells out to ``claude -p`` from this daemon MUST run
:func:`ensure_oauth_token` on the subprocess env it passes.

Resolution order (first hit wins): an existing env value → the **DB secrets
vault**, reached over asa's existing ``PRECIS_DATABASE_URL``. The
vault is what lets asa run as the plain ``deploy`` user with no ``~/.claude``
state. The ``~/.claude_oauth_token`` file this used to read first is retired
fleet-wide — it scattered a live credential in plaintext across service-account
homes and, sitting ahead of the vault, silently shadowed a rotation. Idempotent
and override-safe: a token already present in the env wins; we only fill the
gap.
"""

from __future__ import annotations

import logging
from collections.abc import MutableMapping

log = logging.getLogger(__name__)

#: Env var ``claude`` reads for non-interactive OAuth auth. Also the vault key.
ENV_VAR = "CLAUDE_CODE_OAUTH_TOKEN"

#: Retired per-user token file — named only so the deploy-side purge task and
#: the secret-read guard hook have one definition. Nothing reads it.
LEGACY_TOKEN_FILENAME = ".claude_oauth_token"


def ensure_oauth_token(env: MutableMapping[str, str]) -> None:
    """Fill :data:`ENV_VAR` in ``env`` from the secrets vault.

    Mutates ``env`` in place. No-op when the var is already set to a non-empty
    value (env override wins) or the vault can't resolve it — in that case
    ``claude`` keeps its own resolution order. Best-effort: ``reveal_secret``
    returns None on any error, so an unreachable vault degrades rather than
    raising into the caller's subprocess spawn.
    """
    if env.get(ENV_VAR):
        return
    try:
        from asa_bot.secrets import reveal_secret

        token = reveal_secret(ENV_VAR) or ""
    except Exception:
        log.warning("oauth: vault lookup for %s raised", ENV_VAR)
        token = ""
    if token:
        env[ENV_VAR] = token
        log.debug("oauth: loaded %s from the vault", ENV_VAR)


__all__ = ["ENV_VAR", "LEGACY_TOKEN_FILENAME", "ensure_oauth_token"]
