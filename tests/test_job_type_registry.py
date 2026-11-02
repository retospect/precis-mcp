"""``workers.job_types`` static-name parity.

``_BUILTIN_JOB_TYPES`` is the registry's static-membership half —
``is_registered_job_type`` answers "is this name known" from it
without importing the module (the gripe create ack's offer must not
pay for ``get_job_type``'s load — r185). It must stay in lockstep
with the ``get_job_type`` dispatch chain: a name only in the tuple
would answer "registered" and then fail to resolve; a name only in
the chain would resolve yet never surface in ``known_job_types``
error messages or the offer probe.

Everything here is AST-read (the ``test_alert_ids`` style): the
registry module is parsed, never executed, so the check has no import
side effects and no installed-dist drift.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from precis.workers import job_types


def _tree() -> ast.Module:
    return ast.parse(Path(job_types.__file__).read_text(encoding="utf-8"))


def _str_tuple(tree: ast.Module, name: str) -> set[str] | None:
    """Elements of a module-level ``NAME = ("a", …)`` /
    ``NAME: tuple[str, ...] = (...)`` string literal, else ``None``."""
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(t, ast.Name) and t.id == name for t in targets):
            continue
        if isinstance(node.value, (ast.Tuple, ast.List)):
            return {
                str(e.value) for e in node.value.elts if isinstance(e, ast.Constant)
            }
    return None


def _dispatch_names(tree: ast.Module) -> set[str]:
    """Names ``get_job_type``'s chain resolves: every
    ``if name == "<x>"`` literal plus the ``if name in _RELEASE_OPS``
    / ``if name in _PR_OPS`` legs (read from those module-level
    tuples)."""
    fn = next(
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "get_job_type"
    )
    groups = {g: _str_tuple(tree, g) or set() for g in ("_RELEASE_OPS", "_PR_OPS")}
    names: set[str] = set()
    for sub in ast.walk(fn):
        if not (isinstance(sub, ast.Compare) and len(sub.ops) == 1):
            continue
        left, op, right = sub.left, sub.ops[0], sub.comparators[0]
        if not (isinstance(left, ast.Name) and left.id == "name"):
            continue
        if isinstance(op, ast.Eq) and isinstance(right, ast.Constant):
            names.add(str(right.value))
        elif (
            isinstance(op, ast.In)
            and isinstance(right, ast.Name)
            and right.id in groups
        ):
            names |= groups[right.id]
    return names


def test_builtin_job_types_match_the_dispatch_chain() -> None:
    """Parity: ``_BUILTIN_JOB_TYPES`` names exactly what
    ``get_job_type`` resolves — no more, no less."""
    tree = _tree()
    builtins = _str_tuple(tree, "_BUILTIN_JOB_TYPES")
    assert builtins is not None, "could not AST-read _BUILTIN_JOB_TYPES"
    assert len(builtins) > 40, (
        f"_BUILTIN_JOB_TYPES read back only {len(builtins)} names — "
        "the registry is far larger; the AST reader is probably stale"
    )
    dispatch = _dispatch_names(tree)
    only_in_list = sorted(builtins - dispatch)
    only_in_chain = sorted(dispatch - builtins)
    assert not only_in_list and not only_in_chain, (
        "_BUILTIN_JOB_TYPES and get_job_type's dispatch chain drifted — "
        f"only in the tuple: {only_in_list}; "
        f"only in the chain: {only_in_chain}"
    )


def test_is_registered_job_type_stays_off_plugin_specs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The offer's probe answers from the static set — a builtin never
    reaches plugin discovery (``_entry_points``), and an unknown name
    answers ``False`` through it."""
    assert job_types.is_registered_job_type("fix_gripe")
    assert job_types.is_registered_job_type("cite_resolve")
    monkeypatch.setattr(job_types, "_entry_points", lambda group: [])
    job_types._reset_plugin_cache()
    try:
        assert not job_types.is_registered_job_type("not_a_type")
    finally:
        job_types._reset_plugin_cache()
