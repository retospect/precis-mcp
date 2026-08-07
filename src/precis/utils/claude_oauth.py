"""Bootstrap the long-lived Claude Code OAuth token into a subprocess env.

launchd-spawned daemons (e.g. ``com.precis.worker`` — which, since §A,
also fires the ``dream_agent`` scheduler cadence in-process; the standalone
``com.precis.dream`` daemon it used to run under is retired) don't run any
shell hook, so a ``claude -p`` subprocess they spawn never sees
the ``CLAUDE_CODE_OAUTH_TOKEN`` that an interactive shell would export (see the
note in ``utils/claude_agent``). Without it, ``claude -p`` falls back to the
(possibly stale / revoked) keychain credentials and fails with a
``401 Invalid authentication credentials``.

This is the 2026-07-12 incident: ``claude_agent`` bootstrapped the token, but
``plan_tick`` and ``claude_quota`` each spawned ``claude -p`` with a raw
``dict(os.environ)`` and so authenticated off the stale keychain — every
planner tick and quota refresh 401'd once dispatch recovered. Any code that
shells out to ``claude -p`` from a daemon MUST run :func:`ensure_oauth_token`
on the subprocess env it passes.

**The vault is the only store.** Resolution is env → ``vault.reveal`` (ADR
0055) → ``get_secret``'s own ``~/.secrets/pw/<NAME>`` bootstrap file. The
per-user ``~/.claude_oauth_token`` this module used to read *first* is retired:
it put a live, long-lived credential in plaintext under every service account's
home on every node (a 2026-08-07 fleet sweep found five copies across two
machines, one on a host that runs no agent at all), and being *ahead* of the
vault it silently shadowed a rotation — rotate the vault, and every host with a
stale file kept presenting the old token until someone remembered to delete it
by hand. One store, one rotation point.

Idempotent and override-safe: a token already present in the env (an
interactive shell, a launchd/plist var, an explicit test override) still wins —
we only fill the gap.
"""

from __future__ import annotations

import logging
from collections.abc import MutableMapping

log = logging.getLogger(__name__)

#: Env var ``claude`` reads for non-interactive OAuth auth. Also the vault key
#: the token is stored under — deliberately the same name, so ``get_secret``
#: needs no mapping table and an operator reading a plist sees the same string
#: as ``precis secret get``.
ENV_VAR = "CLAUDE_CODE_OAUTH_TOKEN"

#: Retired per-user token file. Kept only so the deploy-side purge task and the
#: secret-read guard hook have one place to name it; nothing reads it.
LEGACY_TOKEN_FILENAME = ".claude_oauth_token"

#: Env var ``claude`` reads for per-token API-billed auth. When both this and
#: :data:`ENV_VAR` are present the CLI may pick this one — the *expensive* path.
API_KEY_VAR = "ANTHROPIC_API_KEY"


def ensure_oauth_token(env: MutableMapping[str, str]) -> None:
    """Fill :data:`ENV_VAR` in ``env`` from the secrets vault.

    Mutates ``env`` in place. No-op when the var is already set to a non-empty
    value (env override wins) or the vault can't resolve it — in those cases
    ``claude`` keeps its own resolution order.

    ``get_secret`` is env → ``vault.reveal`` → ``~/.secrets/pw/<NAME>`` and is
    best-effort (never raises), so a process with no bound store — a CLI
    one-shot, a test — degrades to the guarded bootstrap file rather than
    failing. Every daemon binds a store at boot, so in the fleet this is the
    vault.
    """
    if env.get(ENV_VAR):
        return
    try:
        from precis import secrets as _secrets

        token = _secrets.get_secret(ENV_VAR) or ""
    except Exception:
        # Defensive only: get_secret swallows its own errors. An import-time
        # failure here must not take down the caller's subprocess spawn.
        log.warning("claude_oauth: vault lookup for %s raised", ENV_VAR)
        token = ""
    if token:
        env[ENV_VAR] = token
        log.debug("claude_oauth: loaded %s from the vault", ENV_VAR)


def prefer_oauth_over_api_key(env: MutableMapping[str, str]) -> str:
    """Make the OAuth token win over ``ANTHROPIC_API_KEY`` when both could auth
    ``claude -p``. Call *after* :func:`ensure_oauth_token`.

    OAuth is the Max-subscription path (no per-token charge); the API key bills
    at API rates. When both are present the CLI can pick the billed key, so with
    an OAuth token available we drop the key from ``env`` — the CLI then can't
    choose the expensive path. Mutates ``env`` in place.

    Returns the resolved auth mode for the caller to log / act on:

    * ``'oauth'`` — an OAuth token is present; ``ANTHROPIC_API_KEY`` scrubbed.
    * ``'api_key'`` — no OAuth token, only the key: the **billed** fallback,
      worth a warning.
    * ``'none'`` — neither; ``claude`` will error (nothing to scrub).

    Callers that *want* the API key (``claude_agent(bare=True)`` in a container
    where OAuth is unreachable) must NOT call this.
    """
    if env.get(ENV_VAR):
        env.pop(API_KEY_VAR, None)
        return "oauth"
    if env.get(API_KEY_VAR):
        return "api_key"
    return "none"


__all__ = [
    "API_KEY_VAR",
    "ENV_VAR",
    "LEGACY_TOKEN_FILENAME",
    "ensure_oauth_token",
    "prefer_oauth_over_api_key",
]
