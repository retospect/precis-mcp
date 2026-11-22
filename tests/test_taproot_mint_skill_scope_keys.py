"""precis-taproot-mint-help's own ``scope`` examples use enumerated keys.

The skill names the scope keys and says an unknown one draws a
``scope-unknown-key`` advisory; an example that trips that advisory
teaches the key the skill warns against (gr684061).

Pure file read plus ``lint_scope`` — no DB, no embedder.
"""

from __future__ import annotations

import ast
import re

from precis.taproot.sentence_lint import lint_scope
from tests._skill_roots import skill_path

_SCOPE_LITERAL_RE = re.compile(r"""["']scope["']\s*:\s*(\{[^{}]*\})""")


def test_mint_skill_scope_examples_draw_no_unknown_key_advisory() -> None:
    text = skill_path("precis-taproot-mint-help").read_text(encoding="utf-8")
    scopes = [ast.literal_eval(m) for m in _SCOPE_LITERAL_RE.findall(text)]
    assert scopes, "no scope example found — the sweep would pass vacuously"
    flagged = {
        warning
        for scope in scopes
        for warning in lint_scope(scope)
        if warning.startswith("scope-unknown-key")
    }
    assert not flagged, sorted(flagged)
