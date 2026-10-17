"""Enforcer for invariants/safe-get-for-agent-urls (docs/conventions/invariants.md).

Agent-supplied URLs go through ``safe_get``/``safe_stream`` (SSRF guard,
revalidates every redirect hop). A raw HTTP client that *follows redirects*
is the bypass: the first hop is vetted by nobody and a 302 can land on a
link-local address. So outside ``utils/safe_fetch.py`` any call that sets
``follow_redirects`` to something other than a literal ``False`` (or uses
``requests``, which follows by default) must be on the reasoned allowlist
below. A NEW file doing it fails here.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"

#: Reviewed fixed-host users (docs/conventions/reviewed-non-problems.md:
#: "Raw httpx on fixed keyless API hosts ... redirects justified in-code").
#: Key = path relative to ``src/``; value = why it is not an agent-URL path.
_ALLOWLIST: dict[str, str] = {
    "precis/utils/safe_fetch.py": "the guard itself",
    "precis/utils/http.py": (
        "http_client() factory; passes the caller's follow_redirects, default "
        "False, and installs the SSRF transport"
    ),
    "precis/handlers/_edgar_client.py": "fixed sec.gov hosts; redirects justified in-code",
    "precis/workers/health_digest.py": "fixed operator-configured status endpoints, not agent URLs",
}

_REQUESTS_CALLS = {
    "get",
    "post",
    "put",
    "delete",
    "head",
    "patch",
    "request",
    "Session",
}


def redirect_followers(source: str) -> list[int]:
    """Line numbers of calls that follow redirects (or use ``requests``)."""
    hits: set[int] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg == "follow_redirects" and not (
                isinstance(kw.value, ast.Constant) and kw.value.value is False
            ):
                hits.add(node.lineno)
        parts = ast.unparse(node.func).split(".")
        if len(parts) == 2 and parts[0] == "requests" and parts[1] in _REQUESTS_CALLS:
            hits.add(node.lineno)
    return sorted(hits)


def test_checker_flags_synthetic_violations() -> None:
    assert redirect_followers(
        "import httpx\nhttpx.Client(follow_redirects=True)\n"
    ) == [2]
    assert redirect_followers(
        "import httpx\nhttpx.get(u, follow_redirects=flag)\n"
    ) == [2]
    assert redirect_followers("import requests\nrequests.get(u)\n") == [2]
    assert (
        redirect_followers("import httpx\nhttpx.Client(follow_redirects=False)\n") == []
    )


def test_no_unreviewed_redirect_following_clients() -> None:
    offenders: list[str] = []
    for p in sorted(_SRC.rglob("*.py")):
        rel = p.relative_to(_SRC).as_posix()
        if rel in _ALLOWLIST:
            continue
        offenders += [
            f"{rel}:{n}" for n in redirect_followers(p.read_text(encoding="utf-8"))
        ]
    assert not offenders, (
        "raw client follows redirects outside utils/safe_fetch.py "
        "(use safe_get/safe_stream for agent-supplied URLs, or add a reasoned "
        f"entry to _ALLOWLIST if the host is a fixed constant): {offenders}"
    )


def test_allowlist_entries_exist() -> None:
    gone = [r for r in _ALLOWLIST if not (_SRC / r).exists()]
    assert not gone, f"stale allowlist entries: {gone}"
