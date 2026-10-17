"""Enforcers for kind-enablement/* (docs/conventions/kind-enablement.md).

* ``declare-requires-env``: ``dispatch.py`` must not guard a ``_gated``/
  ``_try`` boot call behind an inline ``os.environ`` / ``os.getenv`` check;
  the kind declares ``KindSpec.requires_env`` and the gate decides (so the
  ``Kinds unavailable:`` banner stays truthful).
* ``requires-secret-for-credentials``: a handler module that resolves a
  vault credential by literal name (``get_secret("X")`` ...) must list ``X``
  in its ``KindSpec.requires_secret`` / ``requires_env``, unless the
  credential is optional and the reason is recorded in ``_OPTIONAL`` below.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src" / "precis"

_BOOT_CALLS = {"_gated", "_try"}
_SECRET_READERS = {
    "get_secret",
    "require_secret",
    "reveal_unbound",
    "require_vault_secret",
}

#: (handler file, secret) pairs read without being a gate, with the reason.
_OPTIONAL: dict[tuple[str, str], str] = {
    ("semanticscholar.py", "SEMANTIC_SCHOLAR_API_KEY"): (
        "optional: raises the rate limit; the kind works keyless"
    ),
    ("perplexity.py", "PERPLEXITY_API_KEY"): (
        "put(mode='import') registers a $0 web-UI answer and needs no key; "
        "only the paid call path reads it"
    ),
}


def _is_env_read(node: ast.AST) -> bool:
    if isinstance(node, ast.Call):
        return ast.unparse(node.func) in {"os.environ.get", "os.getenv", "environ.get"}
    if isinstance(node, ast.Subscript):
        return ast.unparse(node.value) in {"os.environ", "environ"}
    return False


def inline_env_gates(source: str) -> list[int]:
    """Line numbers of ``if <env-derived>:`` blocks that call ``_gated``/``_try``."""
    hits: list[int] = []
    for fn in ast.walk(ast.parse(source)):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        env_names: set[str] = set()
        for n in ast.walk(fn):
            if isinstance(n, ast.Assign) and any(
                _is_env_read(x) for x in ast.walk(n.value)
            ):
                env_names |= {t.id for t in n.targets if isinstance(t, ast.Name)}
        for n in ast.walk(fn):
            if not isinstance(n, ast.If):
                continue
            guarded = any(_is_env_read(x) for x in ast.walk(n.test)) or any(
                isinstance(x, ast.Name) and x.id in env_names for x in ast.walk(n.test)
            )
            calls_boot = any(
                isinstance(c, ast.Call)
                and isinstance(c.func, ast.Name)
                and c.func.id in _BOOT_CALLS
                for b in n.body
                for c in ast.walk(b)
            )
            if guarded and calls_boot:
                hits.append(n.lineno)
    return sorted(hits)


def _strings(node: ast.AST, consts: dict[str, ast.AST]) -> set[str]:
    out: set[str] = set()
    for x in ast.walk(node):
        if isinstance(x, ast.Constant) and isinstance(x.value, str):
            out.add(x.value)
        elif isinstance(x, ast.Name) and x.id in consts:
            out |= {
                y.value
                for y in ast.walk(consts[x.id])
                if isinstance(y, ast.Constant) and isinstance(y.value, str)
            }
    return out


def undeclared_secrets(source: str) -> set[str]:
    """Literal secret names read in a KindSpec-bearing module but not declared."""
    tree = ast.parse(source)
    consts: dict[str, ast.AST] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                if isinstance(t, ast.Name):
                    consts[t.id] = stmt.value
        elif (
            isinstance(stmt, ast.AnnAssign)
            and isinstance(stmt.target, ast.Name)
            and stmt.value is not None
        ):
            consts[stmt.target.id] = stmt.value
    declared: set[str] = set()
    has_spec = False
    read: set[str] = set()
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        fname = (
            n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", "")
        )
        if fname == "KindSpec":
            has_spec = True
            for kw in n.keywords:
                if kw.arg in {"requires_secret", "requires_env"}:
                    declared |= _strings(kw.value, consts)
        elif fname in _SECRET_READERS and n.args:
            a = n.args[0]
            if isinstance(a, ast.Constant) and isinstance(a.value, str):
                read.add(a.value)
    return (read - declared) if has_spec else set()


def test_inline_gate_checker_flags_synthetic_violation() -> None:
    bad = (
        "def boot():\n"
        "    key = os.environ.get('FOO_API_KEY')\n"
        "    if key:\n"
        "        _gated(FooHandler, key=key)\n"
    )
    assert inline_env_gates(bad) == [3]
    assert inline_env_gates(
        "def boot():\n    if os.getenv('X'):\n        _try(Foo)\n"
    ) == [2]
    # env read as a parameter, not a gate: fine
    assert (
        inline_env_gates(
            "def boot():\n    n = int(os.environ.get('N', '0'))\n    _gated(Foo, n=n)\n"
        )
        == []
    )


def test_dispatch_has_no_inline_env_gating() -> None:
    src = (_SRC / "dispatch.py").read_text(encoding="utf-8")
    assert not inline_env_gates(src), (
        "dispatch.py guards a boot call behind os.environ at lines "
        f"{inline_env_gates(src)}: declare KindSpec.requires_env instead"
    )


def test_secret_checker_flags_synthetic_violation() -> None:
    bad = (
        "class H:\n"
        "    spec = KindSpec(kind='x')\n"
        "    def f(self):\n"
        "        return get_secret('FOO_TOKEN')\n"
    )
    assert undeclared_secrets(bad) == {"FOO_TOKEN"}
    ok = bad.replace(
        "KindSpec(kind='x')", "KindSpec(kind='x', requires_secret=('FOO_TOKEN',))"
    )
    assert undeclared_secrets(ok) == set()
    via_const = "_S = ('FOO_TOKEN',)\n" + bad.replace(
        "KindSpec(kind='x')", "KindSpec(kind='x', requires_secret=_S)"
    )
    assert undeclared_secrets(via_const) == set()


def test_handler_credentials_are_declared() -> None:
    offenders: list[str] = []
    for p in sorted((_SRC / "handlers").glob("*.py")):
        for name in sorted(undeclared_secrets(p.read_text(encoding="utf-8"))):
            if (p.name, name) not in _OPTIONAL:
                offenders.append(f"{p.name}: {name}")
    assert not offenders, (
        "handler reads a credential its KindSpec does not gate on; add it to "
        "requires_secret (or record why it is optional in _OPTIONAL): "
        f"{offenders}"
    )


def test_optional_entries_are_live() -> None:
    live = {
        (p.name, n)
        for p in (_SRC / "handlers").glob("*.py")
        for n in undeclared_secrets(p.read_text(encoding="utf-8"))
    }
    stale = sorted(set(_OPTIONAL) - live)
    assert not stale, f"_OPTIONAL entries no longer needed: {stale}"
