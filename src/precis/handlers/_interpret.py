"""Forgiving inputs: run the unambiguous near-miss and say how it was read.

gr494831 step 2. When a call's intent has exactly one reading (an alias, a
near-miss enum value, an obvious wrong key), the handler runs the canonical
call instead of refusing, and the reply's first line names the reading:
``interpreted: view='full' read as view='comments'``. An input with two
plausible readings still refuses with ``options=``.

The note goes at the top of the body, not on the HintBus: hints are deduped
by topic and capped per response, so a repeated reading would go unannounced.
A note at the top also stays on page 1 when pagination splits the reply.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from precis.response import Response

PREFIX = "interpreted: "


def interpreted(resp: Response, readings: Sequence[str]) -> Response:
    """``resp`` with one leading ``interpreted: …`` line, or unchanged."""
    if not readings:
        return resp
    return replace(resp, body=f"{PREFIX}{'; '.join(readings)}\n{resp.body}")
