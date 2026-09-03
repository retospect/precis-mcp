"""hintcheck — shared round-trip lint for ``Next:`` trailer hints.

Every handler view that advertises follow-up calls in a ``Next:`` trailer
promises the reader that copying a hint works. These helpers turn that
promise into a test: extract every advertised call from a rendered body,
run each through the real command parser, and (for fully-concrete hints)
dispatch it against the same handler/store under test.

Conventions:

- A hint containing an angle-bracket placeholder (``<slug>``, ``<topic>``)
  is a template: it must still *parse* as a command, but is not executed.
- Only read verbs (``get``/``search``) are executed by default — a write
  hint executing against the fixture store would mutate state mid-test;
  pass ``execute_verbs`` to widen when the test wants that.
- Hints are pulled from the first ``Next:`` marker onwards by default;
  pass ``whole_body=True`` for views that hand-roll hint lines elsewhere
  in the body.

The invariant these tests guard lives in the
``precis.utils.next_block`` module docstring.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Any

from precis.tools.command_parser import parse_command

# One-level-nested parens is enough for every hint shape in the codebase
# (args={'page': 2} has braces, not parens; no hint nests calls).
_CALL_RE = re.compile(
    r"\b(?:get|search|put|edit|delete|tag|link)"
    r"\([^()]*(?:\([^()]*\)[^()]*)*\)"
)


def extract_hints(body: str, *, whole_body: bool = False) -> list[str]:
    """Return every advertised verb-call snippet in ``body``'s trailer."""
    if whole_body:
        text = body
    else:
        idx = body.find("Next:")
        text = body[idx:] if idx != -1 else ""
    return _CALL_RE.findall(text)


def is_template(hint: str) -> bool:
    """True when the hint carries an angle-bracket placeholder."""
    return "<" in hint


def assert_hints_round_trip(
    body: str,
    dispatch: Callable[[str, dict[str, Any]], Any],
    *,
    whole_body: bool = False,
    expect_hints: bool = True,
    execute_verbs: Sequence[str] = ("get", "search"),
) -> list[str]:
    """Parse every hint in ``body``; execute the concrete read ones.

    ``dispatch(verb, kwargs)`` runs the call against the fixture under
    test and returns something truthy (typically the response body).
    Returns the extracted hints so callers can add view-specific asserts.
    """
    hints = extract_hints(body, whole_body=whole_body)
    if expect_hints:
        assert hints, f"no verb-call hints found in body:\n{body}"
    for hint in hints:
        verb, kwargs = parse_command(hint)  # raises on non-literal args
        if is_template(hint) or verb not in execute_verbs:
            continue
        result = dispatch(verb, kwargs)
        assert result, f"advertised hint {hint!r} returned an empty result"
    return hints
