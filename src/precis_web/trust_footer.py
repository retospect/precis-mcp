"""The web face of the ``get`` trust footer (gr496226).

``get`` appends ``trust: {stamp} · sha {sha} — edit(...)`` to a
single-ref body — the agent's door to a consider act, but dead call
syntax to a person. Pages that render that body lift the line out and
show the stamp plus a link instead: the console with the same call
prefilled for the operator. The ref's fisheye page stays one click
away via the nav's eye link — the footer doesn't duplicate it.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urlencode

from precis.runtime.trust_verb import TrustFooter, trust_footer

log = logging.getLogger(__name__)


def _console_href(footer: TrustFooter) -> str:
    """A ``/console`` deep-link staging the consider call (``verb=edit``
    prefills the form; a GET never runs a write). ``args=`` rides the
    console's JSON channel; ``reason`` stays empty — submitting without
    one errors rather than recording a placeholder."""
    args_text = (
        f"kind={footer.kind} id={footer.ref_id} mode=trust "
        f"args='{json.dumps({'trust': {'confidence': 'considered', 'reason': '', 'expected_sha': footer.sha}})}'"
    )
    return "/console?" + urlencode({"verb": "edit", "args_text": args_text})


def strip_trust_footer(
    store: Any, kind: str, ident: Any, body: str
) -> tuple[str, dict[str, Any] | None]:
    """Lift the dispatcher-appended trust line out of ``body``.

    Returns ``(body, ctx)`` — ctx ``None`` when the line is absent
    (unconverted kind, an ident the dispatcher skipped, an error body),
    so the caller renders unchanged. The ctx carries the stamp plus the
    links the page shows in the line's place: ``console_href`` stages the
    consider call for the operator — ``None`` when there is no sha to
    name."""
    if not body:
        return body, None
    try:
        footer = trust_footer(store, kind, ident)
    except Exception:  # best-effort, like dispatch's own trust-line append
        log.debug("trust footer for %s %r failed", kind, ident, exc_info=True)
        return body, None
    if footer is None:
        return body, None
    marker = f"\n\n{footer.line}"
    idx = body.rfind(marker)
    if idx < 0:
        return body, None
    return (
        body[:idx] + body[idx + len(marker) :],
        {
            "stamp": footer.stamp,
            "sha": footer.sha,
            "console_href": _console_href(footer) if footer.sha else None,
        },
    )
