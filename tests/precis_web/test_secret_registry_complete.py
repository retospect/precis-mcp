"""Every secret name the code reads has a ``SecretSpec`` on the /secrets page.

A key the code resolves but the registry does not list can neither be
provisioned from the page nor probed by the weekly credential check
(``/whatneedsdoing``), so its expiry surfaces only as a failing job
(gr496305). The scan is static: it reads ``src/`` with ``ast`` and never
resolves a secret.

Names it can see: string literals, and bare names bound to a string constant
at module level anywhere in ``src/`` (``MP_API_KEY_SECRET``). Computed names
(f-strings, ``NAME:<login>`` per-user credentials, loop variables) are
invisible to it by design; per-user credentials are managed on ``/account``.
"""

from __future__ import annotations

import ast
from pathlib import Path

from precis_web import secret_status

_SRC = Path(__file__).resolve().parents[2] / "src"

_READERS = frozenset({"get_secret", "require_secret", "reveal_unbound"})

#: Secrets the code reads that are deliberately not on the page, each with
#: the reason. Adding a name here needs the same justification as a SecretSpec.
_NOT_LISTED: dict[str, str] = {}


def _py_files() -> list[Path]:
    return sorted(_SRC.rglob("*.py"))


def _strs(value: ast.expr) -> set[str]:
    """String constants a module-level value holds: a literal, or a dict's values."""
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return {value.value}
    if isinstance(value, ast.Dict):
        return {
            v.value
            for v in value.values
            if isinstance(v, ast.Constant) and isinstance(v.value, str)
        }
    return set()


def _module_constants(tree: ast.Module) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            targets, value = stmt.targets, stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            targets, value = [stmt.target], stmt.value
        else:
            continue
        for t in targets:
            if isinstance(t, ast.Name):
                out.setdefault(t.id, set()).update(_strs(value))
    return out


def _callee(call: ast.Call) -> str:
    f = call.func
    return f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")


def read_secret_names() -> dict[str, set[str]]:
    """secret name -> ``{path:line}`` sites, for every statically visible read."""
    trees = {p: ast.parse(p.read_text(encoding="utf-8")) for p in _py_files()}
    local = {p: _module_constants(t) for p, t in trees.items()}
    # A name imported from another module resolves against every module's
    # constant of that name; a name the module defines itself never does
    # (``ENV_VAR`` is a different string in each of several modules).
    imported: dict[str, set[str]] = {}
    for consts in local.values():
        for name, values in consts.items():
            imported.setdefault(name, set()).update(values)
    found: dict[str, set[str]] = {}
    for path, tree in trees.items():
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call) and _callee(node) in _READERS and node.args
            ):
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Subscript):  # VAULT_SECRET[role]
                arg = arg.value
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                names = {arg.value}
            elif isinstance(arg, ast.Name):
                names = local[path].get(arg.id) or imported.get(arg.id, set())
            else:
                continue
            site = f"{path.relative_to(_SRC)}:{node.lineno}"
            for name in names:
                found.setdefault(name, set()).add(site)
    return found


def test_scan_sees_the_known_readers() -> None:
    names = read_secret_names()
    assert {"ANTHROPIC_API_KEY", "PRECIS_MP_API_KEY", "ORCID_CLIENT_ID"} <= set(names)


def test_every_secret_the_code_reads_has_a_spec() -> None:
    registry = {s.name for s in secret_status.KNOWN_SECRETS}
    missing = {
        name: sorted(sites)
        for name, sites in read_secret_names().items()
        if name not in registry and name not in _NOT_LISTED
    }
    assert not missing, (
        "get_secret() names with no SecretSpec in precis_web.secret_status."
        f"KNOWN_SECRETS (add one, or justify it in _NOT_LISTED): {missing}"
    )


def test_not_listed_entries_are_still_read_and_still_unlisted() -> None:
    registry = {s.name for s in secret_status.KNOWN_SECRETS}
    read = read_secret_names()
    for name, reason in _NOT_LISTED.items():
        assert reason.strip()
        assert name in read, f"{name} is no longer read; drop it from _NOT_LISTED"
        assert name not in registry, f"{name} has a spec now; drop it from _NOT_LISTED"
