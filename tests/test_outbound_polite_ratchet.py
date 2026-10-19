"""Ratchet for invariants/outbound-calls-are-polite (docs/conventions/invariants.md).

Every outbound HTTP call in ``src/`` (httpx, requests, urllib, aiohttp, or a
client built with ``http_client()``) must run under the politeness core in
``utils/http.py``: ``with_backoff`` / ``call_with_backoff`` (limiter token per
attempt, full jitter, ``Retry-After``, breaker, honest User-Agent) or one of
its thin wrappers (``polite_send``, ``polite_fetch``, ``paced_requests``,
``external_retry``). ``safe_get``/``safe_stream`` are SSRF-only: they do not
count, the call around them still has to be polite.

The check is per outermost enclosing function: a call counts as polite when
that function also references a core name. A private (``_``-prefixed) helper
is the injected transport that a polite caller hands to the core, so for it a
core reference anywhere in the same file is enough. A NEW unpolite site fails here;
fix it (add a ``POLICIES`` entry and route through the core) rather than
widening ``_ALLOWLIST``, which is only for internal or operator endpoints
the cluster owns and that nobody else rate-limits.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"

#: Names whose presence in the enclosing function makes a call polite.
_POLITE_NAMES = frozenset(
    {
        "with_backoff",
        "call_with_backoff",
        "polite_send",
        "polite_fetch",
        "polite_safe_get",
        "async_call_with_backoff",
        "paced_requests",
        "limiter_pacer",
        "external_retry",
    }
)

#: Reviewed internal / operator endpoints: cluster-owned services and
#: operator-configured URLs, not third parties, so a retry-with-backoff
#: against them is the caller's own liveness logic. Key = path relative to
#: ``src/``; value = why it is exempt. Do not add a third-party API here.
_ALLOWLIST: dict[str, str] = {
    "precis/utils/http.py": "the core itself (http_client factory, paced sender)",
    "precis/utils/safe_fetch.py": "the SSRF guard; the caller supplies the politeness",
    "precis/embedder.py": "cluster-internal embedder service",
    "precis/workers/llm_serving.py": "cluster-internal LLM serving endpoints",
    "precis/workers/capability_probe.py": "cluster-internal embedder readiness probe",
    "precis/fixer/tick.py": "operator-configured readyz URL of our own server",
    # Deadman ping: exactly one GET per health-digest tick (the tick is the
    # cadence), to the operator's own heartbeat endpoint. It bypasses the core
    # on purpose: a retry or breaker would blur "the tick ran" into "the
    # network was up", which is the one fact the switch exists to report.
    "precis/workers/health_digest.py": "operator-configured status and liveness endpoints",
    "precis_dft/pourbaix_bulk.py": (
        "precis_dft imports nothing from precis; precis callers inject a polite "
        "get= (pinned by test_pourbaix_bulk_callers_inject_get)"
    ),
    "precis_chem/jobs.py": "cluster-internal ASKCOS service",
}

_CLIENT_CTORS = {
    "http_client",
    "httpx.Client",
    "httpx.AsyncClient",
    "aiohttp.ClientSession",
    "requests.Session",
}
_METHODS = {"get", "post", "put", "delete", "head", "patch", "request", "stream"}


def _is_outbound(call: ast.Call) -> bool:
    name = ast.unparse(call.func)
    if name in _CLIENT_CTORS:
        return True
    parts = name.split(".")
    if len(parts) == 2 and parts[0] in {"httpx", "requests"} and parts[1] in _METHODS:
        return True
    return name in {"urllib.request.urlopen", "urlopen", "urllib.request.Request"}


def _names_in(node: ast.AST) -> set[str]:
    out: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
    return out


#: Qualified polite methods: a class deriving from the key whose body calls
#: the named method is under the core. Deliberately not a bare name in
#: ``_POLITE_NAMES``: any function called ``_roundtrip`` would then pass.
_POLITE_METHODS = {"JlcApiClient": "_roundtrip"}  # → with_backoff (pcb/jlc_api.py)


def _uses_polite_method(tree: ast.AST) -> bool:
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        for base in cls.bases:
            method = _POLITE_METHODS.get(ast.unparse(base).rsplit(".", 1)[-1])
            if method and method in _names_in(cls):
                return True
    return False


def unpolite_calls(source: str) -> list[int]:
    """Line numbers of outbound calls not under a politeness-core name."""
    tree = ast.parse(source)
    hits: list[int] = []
    file_polite = bool(_names_in(tree) & _POLITE_NAMES) or _uses_polite_method(tree)

    def visit(node: ast.AST, scope: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            inner = scope
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                # Outermost function wins; class bodies don't open a scope.
                inner = scope if scope is not tree else child
            if isinstance(child, ast.Call) and _is_outbound(child):
                private = isinstance(
                    inner, ast.FunctionDef | ast.AsyncFunctionDef
                ) and inner.name.startswith("_")
                if not (_names_in(inner) & _POLITE_NAMES) and not (
                    private and file_polite
                ):
                    hits.append(child.lineno)
            visit(child, inner)

    visit(tree, tree)
    return sorted(set(hits))


def test_checker_flags_synthetic_violations() -> None:
    assert unpolite_calls("import httpx\ndef f():\n    return httpx.get(u)\n") == [3]
    assert unpolite_calls("import requests\ndef f():\n    requests.post(u)\n") == [3]
    assert unpolite_calls(
        "import urllib.request\ndef f():\n    urllib.request.urlopen(r)\n"
    ) == [3]
    # safe_get is SSRF-only: the client construction is still unpolite.
    assert unpolite_calls(
        "def f():\n    with http_client() as c:\n        return safe_get(c, u)\n"
    ) == [2]
    assert (
        unpolite_calls(
            "def f():\n    return call_with_backoff('s2', lambda: httpx.get(u))\n"
        )
        == []
    )
    assert (
        unpolite_calls(
            "class C(jlc_api.JlcApiClient):\n    def up(self):\n"
            "        self._roundtrip(t)\ndef _send():\n    httpx.post(u)\n"
        )
        == []
    )
    assert unpolite_calls(
        "class C(Other):\n    def up(self):\n        self._roundtrip(t)\n"
        "def _send():\n    httpx.post(u)\n"
    ) == [5]
    # A private injected transport is polite when its file uses the core.
    assert (
        unpolite_calls(
            "def _send():\n    return httpx.get(u)\ndef f():\n    with_backoff(_send)\n"
        )
        == []
    )
    assert unpolite_calls(
        "def send():\n    return httpx.get(u)\ndef f():\n    with_backoff(send)\n"
    ) == [2]
    assert (
        unpolite_calls(
            "class A:\n    def m(self):\n        with_backoff(h)\n"
            "        with http_client() as c:\n            pass\n"
        )
        == []
    )


def test_every_outbound_call_is_polite() -> None:
    offenders: list[str] = []
    for p in sorted(_SRC.rglob("*.py")):
        rel = p.relative_to(_SRC).as_posix()
        if rel in _ALLOWLIST:
            continue
        offenders += [
            f"{rel}:{n}" for n in unpolite_calls(p.read_text(encoding="utf-8"))
        ]
    assert not offenders, (
        "outbound HTTP call outside the politeness core (route it through "
        "utils/http.py with_backoff/call_with_backoff and a POLICIES entry; "
        "see docs/conventions/invariants.md 'Outbound calls are polite'): "
        f"{offenders}"
    )


def test_allowlist_entries_exist_and_are_reasoned() -> None:
    gone = [r for r in _ALLOWLIST if not (_SRC / r).exists()]
    assert not gone, f"stale allowlist entries: {gone}"
    assert all(len(why) > 10 for why in _ALLOWLIST.values())


def _unpinned_fetch_calls(source: str) -> list[int]:
    """Lines calling precis_dft's MP fetchers without a ``get=`` argument."""
    return sorted(
        n.lineno
        for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.Call)
        and ast.unparse(n.func).rsplit(".", 1)[-1]
        in {"fetch_entries", "fetch_ion_reference_data"}
        and not any(kw.arg == "get" for kw in n.keywords)
    )


def test_pourbaix_bulk_callers_inject_get() -> None:
    """precis_dft imports nothing from precis, so it cannot be polite itself;
    every precis caller must inject a polite ``get=`` (the allowlist reason)."""
    assert _unpinned_fetch_calls("fetch_entries(k, c)\n") == [1]
    assert _unpinned_fetch_calls("fetch_entries(k, c, get=g)\n") == []
    bad: list[str] = []
    for p in sorted((_SRC / "precis").rglob("*.py")):
        bad += [
            f"{p.relative_to(_SRC).as_posix()}:{n}"
            for n in _unpinned_fetch_calls(p.read_text(encoding="utf-8"))
        ]
    assert not bad, f"precis_dft MP fetch without a polite get=: {bad}"
