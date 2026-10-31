"""The factory's git/GitHub credential for the jobs that move refs.

``land_slice`` (``scripts/ship``: fetch, the CAS push to ``main``, the
branch delete, the PR close), ``release_cut`` / ``release_deploy``
(``scripts/release``: the ``release/r<N>``, ``gated`` and ``prod`` pushes)
and ``pr_action`` (``gh``) run on a slice host as a service account whose
checkout carries no credential of its own (melchior's ``deploy`` user:
``/Users/deploy/precis-fix-repo``, td543734). The credential is the vault
secret :data:`SECRET_NAME` — a fine-grained GitHub token (Reto, 2026-10-31:
one token for slices and lander alike while the loop closes once; the
slice/\\* -only token and the ruleset come later) — and it reaches git
through a credential helper the process env installs, never through argv
or a file:

* ``GIT_CONFIG_COUNT`` / ``GIT_CONFIG_KEY_n`` / ``GIT_CONFIG_VALUE_n`` reset
  the helper list and add one helper for ``https://github.com`` that
  answers ``get`` by reading the token back out of the env;
* ``GH_TOKEN`` for ``gh``;
* the token itself under :data:`SECRET_NAME`, for the helper to read.

The slice *session* never gets this env: ``run_slice.slice_env`` is an
allowlist and the brief says "do not push" — the builder's branch leaves
the host only through the lander. Resolution is env → vault
(``secrets.get_secret`` / ``reveal_unbound``, like the OAuth token); with
no token the env is returned unchanged and the host's own credentials, if
any, apply.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping

log = logging.getLogger(__name__)

#: Vault key and env name of the token (the same string, like the OAuth token).
SECRET_NAME = "PRECIS_FACTORY_GIT_TOKEN"

#: Username the helper answers with; a GitHub token authenticates by password.
_HELPER_USER = "x-access-token"

#: The helper. ``sh`` runs it; it reads the token from the env, so neither
#: ``ps`` nor the git config carries it.
_HELPER = (
    "!f() { [ \"$1\" = get ] && printf 'username=%s\\npassword=%s\\n' "
    f"'{_HELPER_USER}' \"${SECRET_NAME}\"; }}; f"
)


def factory_git_token() -> str | None:
    """The token, env first, else the vault; ``None`` when neither has it."""
    env = os.environ.get(SECRET_NAME, "").strip()
    if env:
        return env
    try:
        from precis import secrets as _secrets

        token = _secrets.get_secret(SECRET_NAME) or _secrets.reveal_unbound(SECRET_NAME)
    except Exception:  # pragma: no cover — get_secret swallows its own errors
        log.warning("factory_git: vault lookup for %s raised", SECRET_NAME)
        return None
    return token.strip() if token else None


def git_auth_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """``base`` (default ``os.environ``) plus the credential wiring, or
    ``base`` unchanged (with a warning) when no token resolves. Existing
    ``GIT_CONFIG_*`` entries are kept; ours are appended after them."""
    env = dict(os.environ if base is None else base)
    token = factory_git_token()
    if not token:
        log.warning(
            "factory_git: no %s in env or vault — git/gh run on the host's own "
            "credentials (pushes from a service account will be refused)",
            SECRET_NAME,
        )
        return env
    try:
        n = int(env.get("GIT_CONFIG_COUNT", "0") or 0)
    except ValueError:
        n = 0
    env[SECRET_NAME] = token
    env["GH_TOKEN"] = token
    # Empty value: reset every helper configured so far (system, global,
    # local — e.g. osxkeychain answering with another account), then ours.
    env[f"GIT_CONFIG_KEY_{n}"] = "credential.helper"
    env[f"GIT_CONFIG_VALUE_{n}"] = ""
    env[f"GIT_CONFIG_KEY_{n + 1}"] = "credential.https://github.com.helper"
    env[f"GIT_CONFIG_VALUE_{n + 1}"] = _HELPER
    env["GIT_CONFIG_COUNT"] = str(n + 2)
    return env


__all__ = ["SECRET_NAME", "factory_git_token", "git_auth_env"]
