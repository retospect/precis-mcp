"""``_safe_local_redirect`` — the ``return_to`` open-redirect guard (gr541794).

Pure unit tests over the helper in ``routes/flags.py``; the routes that
call it are exercised through the fake-store client elsewhere. The hole:
browsers normalise ``\\`` to ``/``, so ``/\\host`` collapses into the
protocol-relative ``//host`` — the old ``startswith("//")`` check let it
through.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from precis_web.routes.flags import _safe_local_redirect

FALLBACK = "/drive"


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("/ok", "/ok"),
        ("/ok?x=1", "/ok?x=1"),
        ("/%2F%2Fevil.com", "/%2F%2Fevil.com"),  # percent-encoded stays a path
        ("//evil.com", FALLBACK),
        ("/\\evil.com", FALLBACK),  # browser: /\ → // → open redirect
        ("\\\\evil.com", FALLBACK),
        ("http://evil.com", FALLBACK),
        ("https://evil.com/x", FALLBACK),
        ("", FALLBACK),
        ("ok", FALLBACK),  # relative, no leading slash
        ("/ok\tevil.com", FALLBACK),  # tab inside the path
        ("/ok\nevil.com", FALLBACK),  # newline inside the path
    ],
)
def test_safe_local_redirect(target: str, expected: str) -> None:
    assert _safe_local_redirect(target, FALLBACK) == expected
