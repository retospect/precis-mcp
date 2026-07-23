"""Startup identity check.

A copy-pasted or mislabeled Slack token can resolve to a completely
different app/bot identity than the one you meant to deploy. ``auth.test``
is cheap and authoritative: call it once at boot and log the resolved
identity prominently, so a mismatch is visible on the first line of the
log. This is purely informational by default — an admin may have
registered the Slack app under a name that isn't "asa" (it happened; the
resolved identity here has in fact come back as "ada" before), and that's
not an error. Only refuses to start if the operator has explicitly pinned
an expected bot user id and it doesn't match.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

log = logging.getLogger(__name__)


class IdentityMismatch(RuntimeError):
    pass


class _AuthTestClient(Protocol):
    async def auth_test(self) -> Any: ...


async def check_identity(
    client: _AuthTestClient, *, expected_bot_user_id: str = ""
) -> str:
    """Call ``auth.test``, log the resolved identity, return the bot user id.

    Raises :class:`IdentityMismatch` only when ``expected_bot_user_id`` is
    non-empty and doesn't match what ``auth.test`` returns — an opt-in hard
    check. With no expectation configured (the default), this only logs.
    """
    resp = await client.auth_test()
    bot_user_id = str(resp.get("user_id") or "")
    bot_name = resp.get("user") or "?"
    team = resp.get("team") or "?"
    log.info(
        "asa-slack identity: connected as %r (user_id=%s) in workspace %r",
        bot_name,
        bot_user_id,
        team,
    )
    if expected_bot_user_id and bot_user_id != expected_bot_user_id:
        raise IdentityMismatch(
            f"resolved bot identity {bot_name!r} ({bot_user_id}) does not match "
            f"the configured expected_bot_user_id {expected_bot_user_id!r} — "
            "refusing to start."
        )
    return bot_user_id
