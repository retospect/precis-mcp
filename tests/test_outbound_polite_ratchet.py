"""Ratchet for invariants/outbound-calls-are-polite (docs/conventions/invariants.md).

Every outbound HTTP call in ``src/`` (httpx, requests, urllib, aiohttp, or a
client built with ``http_client()``) must run under the politeness core in
``utils/http.py``: ``with_backoff`` / ``call_with_backoff`` (limiter token per
attempt, full jitter, ``Retry-After``, breaker, honest User-Agent) or one of
its thin wrappers (``polite_send``, ``polite_fetch``, ``paced_requests``,
``external_retry``). ``safe_get``/``safe_stream`` are SSRF-only: they do not
count, the call around them still has to be polite. A shared limiter token
taken in the same function (``rate_limit.require``/``acquire``) also counts —
that is the routed shape ``fetch_oa._query_core_fulltext_urls`` carries
(gr507344: the bare ``http_client().get`` this test used to miss, fixed in
622ff9556).

The check is PER CALL, not per enclosing function — that is what tightened
here (outbound-policy S3). The old rule asked whether the outermost
function (or the file, for ``_``-private helpers) named a core name
anywhere, so one ``with_backoff`` in the file laundered every bare call.
Now each outbound call must resolve to the core itself:

* a call inside a polite call's arguments is polite —
  ``call_with_backoff(lambda: httpx.get(u), service='s2')``;
* a call inside a *routed* function is polite — a private or nested
  function handed to the core (``with_backoff(_send)``, a ``_thunk``
  passed to ``JlcApiClient._roundtrip``) or called by one, plus the
  injected-transport chain: a nested fetcher a factory returns when the
  file invokes a transport parameter inside a routed function
  (``si_discovery``: ``fetch(u)`` inside ``_once`` → ``default_fetch``'s
  ``_fetch`` → ``_http_get``). A module-level PUBLIC function never joins
  the routed set — callers can invoke it bare, so its own calls must
  stand on their own;
* a client/request-object construction (``http_client()``,
  ``httpx.Client()``, ``urllib.request.Request``) is polite when the
  enclosing function is polite-context (itself calls the core or takes a
  limiter), when it is invoked from one, or when the variable/attribute it
  binds flows into a polite call — the construction is not the wire call;
* a wire call (module-level ``httpx.get``/``requests.post``/``urlopen``,
  or ``v.method`` on a variable bound to a client) is polite only inside
  a polite call's arguments, inside a routed function, or in a function
  that takes a limiter token.

A NEW unpolite site fails here; fix it (route through the core, or take a
shared limiter token) rather than widening ``_ALLOWLIST``, which is only
for internal or operator endpoints the cluster owns and that nobody else
rate-limits.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"

#: Names whose call marks a polite context or routes its argument callees:
#: the politeness core and its thin wrappers.
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

#: Shared-limiter acquisitions: a function taking a token before its own
#: request is routed even when no backoff wraps it (the CORE shape).
_LIMITER_NAMES = frozenset(
    {"require_rate_limit", "rate_limit.require", "rate_limit.acquire"}
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
    "precis/workers/serving_metrics.py": "loopback scrape of the host's own SGLang /metrics",
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

#: Client constructors: building one is not the wire call, so a flagged
#: construction is excused when its client provably serves the core.
_CLIENT_CTORS = {
    "http_client",
    "httpx.Client",
    "httpx.AsyncClient",
    "aiohttp.ClientSession",
    "requests.Session",
}
#: ``urllib.request.Request`` builds a request object, not a wire call.
_REQUEST_OBJS = {"urllib.request.Request"}
_METHODS = {"get", "post", "put", "delete", "head", "patch", "request", "stream"}
_ONE_SHOTS = {"urllib.request.urlopen", "urlopen"}

#: Qualified polite methods: a ``self.<method>(thunk)`` call inside a class
#: deriving from the key runs the thunk under the core (the method's own
#: body calls the named core entry). Deliberately not a bare name in
#: ``_POLITE_NAMES``: any function called ``_roundtrip`` would then pass.
_POLITE_METHODS = {"JlcApiClient": "_roundtrip"}  # → with_backoff (pcb/jlc_api.py)


def _call_name(call: ast.Call) -> str:
    return ast.unparse(call.func)


_Fn = ast.FunctionDef | ast.AsyncFunctionDef


def _calls_in(node: ast.AST) -> list[ast.Call]:
    return [n for n in ast.walk(node) if isinstance(n, ast.Call)]


class _File:
    """One parsed module: its functions, class-attribute bindings and the
    two sets the verdicts draw on (polite-context, routed)."""

    def __init__(self, tree: ast.AST) -> None:
        self.tree = tree
        #: Function node -> (name, container node, nested-in-a-function).
        self.funcs: dict[_Fn, tuple[str, ast.AST, bool]] = {}
        self.by_name: dict[str, list[_Fn]] = {}
        #: ``self.<attr> = <expr>`` -> function names the expression may bind.
        self.self_bind: dict[str, set[str]] = {}
        self.polite_ctx: set[_Fn] = set()
        self.routed: set[_Fn] = set()
        self._collect()
        self._classify()

    def _collect(self) -> None:
        def visit(node: ast.AST, container: ast.AST) -> None:
            for ch in ast.iter_child_nodes(node):
                if isinstance(ch, ast.FunctionDef | ast.AsyncFunctionDef):
                    self.funcs[ch] = (
                        ch.name,
                        container,
                        isinstance(container, ast.FunctionDef | ast.AsyncFunctionDef),
                    )
                    self.by_name.setdefault(ch.name, []).append(ch)
                    visit(ch, ch)
                elif isinstance(ch, ast.ClassDef):
                    visit(ch, ch)
                else:
                    visit(ch, container)

        visit(self.tree, self.tree)
        for cls in ast.walk(self.tree):
            if not isinstance(cls, ast.ClassDef):
                continue
            for n in ast.walk(cls):
                if isinstance(n, ast.Assign):
                    for t in n.targets:
                        if (
                            isinstance(t, ast.Attribute)
                            and isinstance(t.value, ast.Name)
                            and t.value.id == "self"
                        ):
                            names = {
                                s.id
                                for s in ast.walk(n.value)
                                if isinstance(s, ast.Name)
                            }
                            self.self_bind.setdefault(t.attr, set()).update(names)

    def _private_or_nested(self, fn: _Fn) -> bool:
        name, _container, nested = self.funcs[fn]
        return name.startswith("_") or nested

    def resolve_callee(self, call: ast.Call) -> list[_Fn]:
        """Functions ``call`` may invoke: same-class methods for ``self.x``,
        else name matches, plus ``self.<attr>``-bound candidates."""
        f = call.func
        out: list[_Fn] = []
        if isinstance(f, ast.Name):
            out += self.by_name.get(f.id, [])
        elif isinstance(f, ast.Attribute):
            out += self.by_name.get(f.attr, [])
            if isinstance(f.value, ast.Name) and f.value.id == "self":
                for nm in self.self_bind.get(f.attr, ()):
                    out += self.by_name.get(nm, [])
        return out

    def _is_polite_method_call(self, call: ast.Call) -> bool:
        f = call.func
        if not (isinstance(f, ast.Attribute) and f.attr in _POLITE_METHODS.values()):
            return False
        for cls in ast.walk(self.tree):
            if isinstance(cls, ast.ClassDef) and any(
                ast.unparse(b).rsplit(".", 1)[-1] in _POLITE_METHODS for b in cls.bases
            ):
                return True
        return False

    def _takes_limiter(self, fn: _Fn) -> bool:
        return any(_call_name(c) in _LIMITER_NAMES for c in _calls_in(fn))

    def _is_polite_call(self, call: ast.Call) -> bool:
        """A call that runs its argument callees under the core: a core name,
        a class polite-method, or a polite-context function (a wrapper like
        ``openai_tools._polite`` or ``wikipedia._get``)."""
        if _call_name(call).rsplit(".", 1)[-1] in _POLITE_NAMES:
            return True
        if self._is_polite_method_call(call):
            return True
        return any(fn in self.polite_ctx for fn in self.resolve_callee(call))

    def _classify(self) -> None:
        """Fixpoint: a function is polite-context when one of its calls is a
        core name, a polite method, a limiter acquisition, or resolves to a
        function already in the set (``_get`` → ``polite_safe_get``)."""
        changed = True
        while changed:
            changed = False
            for fn in self.funcs:
                if fn in self.polite_ctx:
                    continue
                if any(self._is_polite_call(c) for c in _calls_in(fn)) or (
                    self._takes_limiter(fn)
                ):
                    self.polite_ctx.add(fn)
                    changed = True

    def route(self) -> None:
        """Populate ``self.routed``: private/nested functions the core or a
        polite-context function invokes (handed, called inside, via
        ``self.<attr>``), transitively — plus factory-returned fetchers when
        the file demonstrably invokes a transport parameter under the core."""
        for call in _calls_in(self.tree):
            if not self._is_polite_call(call):
                continue
            for arg in list(call.args) + [k.value for k in call.keywords]:
                for s in ast.walk(arg):
                    nm = (
                        s.id
                        if isinstance(s, ast.Name)
                        else s.attr
                        if isinstance(s, ast.Attribute)
                        else None
                    )
                    if nm:
                        for fn in self.by_name.get(nm, []):
                            if self._private_or_nested(fn):
                                self.routed.add(fn)
        # Injection contract: a polite-context function calls a name no file
        # function defines — a transport parameter (``fetch(u)`` in
        # ``si_discovery._once``). When the contract holds, nested fetchers
        # a factory returns count as routed too.
        contract = any(
            isinstance(c.func, ast.Name) and c.func.id not in self.by_name
            for fn in self.polite_ctx | self.routed
            for c in _calls_in(fn)
        )
        if contract:
            for fn, (name, container, nested) in self.funcs.items():
                if not nested:
                    continue
                for r in ast.walk(container):
                    if (
                        isinstance(r, ast.Return)
                        and r.value is not None
                        and any(
                            isinstance(s, ast.Name) and s.id == name
                            for s in ast.walk(r.value)
                        )
                    ):
                        self.routed.add(fn)
        changed = True
        while changed:
            changed = False
            for fn in list(self.routed):
                for c in _calls_in(fn):
                    for g in self.resolve_callee(c):
                        if g not in self.routed and self._private_or_nested(g):
                            self.routed.add(g)
                            changed = True

    def innermost_fn(self, node: ast.Call) -> _Fn | None:
        best: _Fn | None = None
        for fn in self.funcs:
            if fn.lineno <= node.lineno <= (fn.end_lineno or fn.lineno):
                if best is None or fn.lineno > best.lineno:
                    best = fn
        return best

    def bound_ctors(self, fn: _Fn) -> dict[str, str]:
        """Name -> ctor it binds, for ``with Ctor() as v`` / ``v = Ctor()``."""
        out: dict[str, str] = {}
        for n in ast.walk(fn):
            if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call):
                cn = _call_name(n.value)
                if cn in _CLIENT_CTORS | _REQUEST_OBJS:
                    for t in n.targets:
                        if isinstance(t, ast.Name):
                            out[t.id] = cn
            elif isinstance(n, ast.With | ast.AsyncWith):
                for item in n.items:
                    if isinstance(item.optional_vars, ast.Name) and isinstance(
                        item.context_expr, ast.Call
                    ):
                        cn = _call_name(item.context_expr)
                        if cn in _CLIENT_CTORS | _REQUEST_OBJS:
                            out[item.optional_vars.id] = cn
        return out

    def inside_polite_args(self, node: ast.AST) -> bool:
        for c in _calls_in(self.tree):
            if not self._is_polite_call(c):
                continue
            for arg in list(c.args) + [k.value for k in c.keywords]:
                if any(s is node for s in ast.walk(arg)):
                    return True
        return False

    def _var_flows_polite(self, var: str, fn: _Fn) -> bool:
        """``var`` is passed as an argument to a polite call in ``fn`` —
        ``polite_safe_get(client, url)``, ``self._resolve_title(client, …)``."""
        for c in _calls_in(fn):
            if not self._is_polite_call(c):
                continue
            for arg in list(c.args) + [k.value for k in c.keywords]:
                if any(isinstance(s, ast.Name) and s.id == var for s in ast.walk(arg)):
                    return True
        return False

    def _ctor_ok(self, call: ast.Call, fn: _Fn | None) -> bool:
        if fn is None:
            return self.inside_polite_args(call)
        if fn in self.polite_ctx or fn in self.routed:
            return True
        # self.<attr>-bound client whose class contains a polite call.
        for a in ast.walk(fn):
            if (
                isinstance(a, ast.Assign)
                and isinstance(a.value, ast.Call)
                and a.value is call
                and any(
                    isinstance(t, ast.Attribute)
                    and isinstance(t.value, ast.Name)
                    and t.value.id == "self"
                    for t in a.targets
                )
            ):
                for cls in ast.walk(self.tree):
                    if isinstance(cls, ast.ClassDef) and any(
                        self._is_polite_call(c) for c in _calls_in(cls)
                    ):
                        return True
        # A local client var handed into a polite call: the construction
        # exists to serve it (``polite_safe_get(client, url)``).
        for var in self._bound_to(fn, call):
            if self._var_flows_polite(var, fn):
                return True
        # A private factory's result bound at a call site and handed to a
        # polite callee (``_client()`` → ``_get_json(client, …)``).
        if self._private_or_nested(fn):
            fn_name = self.funcs[fn][0]
            for g in self.funcs:
                if g is fn:
                    continue
                for var in self._factory_results(g, fn_name):
                    if self._var_flows_polite(var, g):
                        return True
        return False

    def _bound_to(self, fn: _Fn, call: ast.Call) -> set[str]:
        """Names ``call``'s result binds to inside ``fn`` (with-as/assign)."""
        out: set[str] = set()
        for n in ast.walk(fn):
            if isinstance(n, ast.With | ast.AsyncWith):
                for item in n.items:
                    if item.context_expr is call and isinstance(
                        item.optional_vars, ast.Name
                    ):
                        out.add(item.optional_vars.id)
            elif isinstance(n, ast.Assign) and n.value is call:
                for t in n.targets:
                    if isinstance(t, ast.Name):
                        out.add(t.id)
        return out

    def _factory_results(self, fn: _Fn, factory: str) -> set[str]:
        """Names in ``fn`` bound to a call on ``factory`` (with-as or assign)."""
        out: set[str] = set()
        for n in ast.walk(fn):
            if isinstance(n, ast.With | ast.AsyncWith):
                for item in n.items:
                    if (
                        isinstance(item.context_expr, ast.Call)
                        and _call_name(item.context_expr).rsplit(".", 1)[-1] == factory
                        and isinstance(item.optional_vars, ast.Name)
                    ):
                        out.add(item.optional_vars.id)
            elif isinstance(n, ast.Assign) and isinstance(n.value, ast.Call):
                if _call_name(n.value).rsplit(".", 1)[-1] == factory:
                    for t in n.targets:
                        if isinstance(t, ast.Name):
                            out.add(t.id)
        return out

    def _req_ok(self, call: ast.Call, fn: _Fn | None) -> bool:
        if self.inside_polite_args(call):
            return True
        if fn is None:
            return False
        if fn in self.routed:
            return True
        return self._takes_limiter(fn)

    def unpolite(self) -> list[int]:
        self.route()
        hits: list[int] = []
        for n in ast.walk(self.tree):
            if not isinstance(n, ast.Call):
                continue
            name = _call_name(n)
            kind: str | None = None
            if name in _CLIENT_CTORS or name in _REQUEST_OBJS:
                kind = "ctor"
            else:
                parts = name.split(".")
                if (
                    len(parts) == 2
                    and parts[0] in {"httpx", "requests"}
                    and parts[1] in _METHODS
                ) or name in _ONE_SHOTS:
                    kind = "req"
                elif isinstance(n.func, ast.Attribute) and n.func.attr in _METHODS:
                    v = n.func.value
                    if isinstance(v, ast.Call) and _call_name(v) in _CLIENT_CTORS:
                        kind = "req"  # http_client(...).get — the chained form
                    elif isinstance(v, ast.Name):
                        fn = self.innermost_fn(n)
                        if fn is not None and v.id in self.bound_ctors(fn):
                            kind = "req"
            if kind is None:
                continue
            fn = self.innermost_fn(n)
            ok = self._ctor_ok(n, fn) if kind == "ctor" else self._req_ok(n, fn)
            if not ok:
                hits.append(n.lineno)
        return sorted(set(hits))


def unpolite_calls(source: str) -> list[int]:
    """Line numbers of outbound calls that do not resolve to the core."""
    return _File(ast.parse(source)).unpolite()


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
    # A private function handed to the core routes; a private function of a
    # JlcApiClient file that is NOT handed does not (per call, not per file).
    assert unpolite_calls(
        "class C(jlc_api.JlcApiClient):\n    def up(self):\n"
        "        self._roundtrip(t)\ndef _send():\n    httpx.post(u)\n"
    ) == [5]
    assert unpolite_calls(
        "class C(Other):\n    def up(self):\n        self._roundtrip(t)\n"
        "def _send():\n    httpx.post(u)\n"
    ) == [5]
    # A private injected transport is polite when it is handed to the core.
    assert (
        unpolite_calls(
            "def _send():\n    return httpx.get(u)\ndef f():\n    with_backoff(_send)\n"
        )
        == []
    )
    # A private helper never handed to the core is unpolite — the CORE
    # bypass shape (gr507344) the old per-function check laundered.
    assert unpolite_calls(
        "def _send():\n    return httpx.get(u)\ndef f():\n    _send()\n"
    ) == [2]
    # A module-level public function stays unpolite even when handed —
    # other callers can invoke it bare.
    assert unpolite_calls(
        "def send():\n    return httpx.get(u)\ndef f():\n    with_backoff(send)\n"
    ) == [2]
    # One polite call in the function no longer launders a second bare call.
    assert unpolite_calls("def f():\n    with_backoff(h)\n    httpx.get(u)\n") == [3]
    # A bare client construction plus a bare method call: both flagged.
    assert unpolite_calls(
        "def f():\n    with http_client() as c:\n        return c.get(u)\n"
    ) == [2, 3]
    # The chained construction is the same bypass in one expression.
    assert unpolite_calls("def f():\n    return http_client().get(u)\n") == [2]
    # A limiter token taken in the function routes the bare call (the
    # fixed CORE shape).
    assert (
        unpolite_calls(
            "def _q():\n    require_rate_limit('core')\n"
            "    with http_client() as c:\n        return c.get(u)\n"
        )
        == []
    )
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
        "utils/http.py with_backoff/call_with_backoff and a POLICIES entry, "
        "or take a shared limiter token in the same function; "
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
