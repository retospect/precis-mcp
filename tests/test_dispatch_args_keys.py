"""gr475332: unknown ``args=`` keys are rejected on passthrough handlers.

A handler whose verb method declares ``args: dict`` receives the whole
extras dict instead of flattened kwargs, which used to skip the
unknown-key gate (``todo.search(args={'bogus': 1})`` silently dropped
``bogus``). Such handlers now declare ``ARGS_KEYS[verb]``; dispatch
rejects anything else with the same ``BadInput`` shape as paper.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil
import textwrap
from collections.abc import Callable
from importlib.metadata import entry_points
from typing import Any

import pytest

import precis.handlers as handlers_pkg
from precis.protocol import ArgsSpec, Handler, UndeclaredArgsKey
from precis.runtime import PrecisRuntime
from precis.store import Store

#: (kind, verb) for every in-repo handler verb that declares ``args``.
PASSTHROUGH = [
    ("todo", "search"),
    ("todo", "delete"),
    ("gripe", "delete"),
    ("draft", "get"),
    ("python", "get"),
    ("pcb", "put"),
    ("pcb", "get"),
    ("cad", "get"),
    ("component", "put"),
    ("component", "get"),
    ("quest", "get"),
    ("paper", "get"),
    ("paper", "edit"),
    ("structure", "put"),
    ("structure", "edit"),
    ("structure", "get"),
    ("random", "get"),
    ("time", "get"),
    ("paper", "put"),
    ("datasheet", "put"),
    # Plugin handlers (entry point group ``precis.handlers``).
    ("estimate", "get"),
    ("pathway", "get"),
    ("pathway", "put"),
    ("se", "get"),
    ("se", "put"),
    ("se", "edit"),
]


def _handler_class(runtime: PrecisRuntime, kind: str) -> type[Handler]:
    return type(runtime.hub.handlers[kind])


def _call(runtime: PrecisRuntime, kind: str, verb: str, extras: dict) -> str:
    return runtime.dispatch(verb, {"kind": kind, "__extras__": extras})


@pytest.mark.parametrize(("kind", "verb"), PASSTHROUGH)
def test_junk_args_key_rejected_naming_accepted_keys(
    runtime_with_store: PrecisRuntime, kind: str, verb: str
) -> None:
    if runtime_with_store.hub.handler_for(kind) is None:
        # Gated out in this build (e.g. pathway without autocatpath on CI).
        pytest.skip(f"kind {kind!r} disabled in this build")
    out = _call(runtime_with_store, kind, verb, {"zz_bogus_key": 1})
    assert "[error:BadInput]" in out, out
    assert f"not accepted by {kind}.{verb}" in out, out
    assert "zz_bogus_key" in out
    cls = _handler_class(runtime_with_store, kind)
    for key in cls.ARGS_KEYS[verb]:
        assert key in out, f"accepted key {key!r} missing from error: {out}"


def _declared_cases() -> list[tuple[str, str, str]]:
    # Static import-time view of the declarations (no runtime needed).
    cases: list[tuple[str, str, str]] = []
    for kind, verb in PASSTHROUGH:
        cls = _KIND_CLASS[kind]
        cases.extend((kind, verb, k) for k in sorted(cls.ARGS_KEYS[verb]))
    return cases


def _load_kind_classes() -> dict[str, type[Handler]]:
    out: dict[str, type[Handler]] = {}
    for mod in pkgutil.iter_modules(handlers_pkg.__path__):
        m = importlib.import_module(f"precis.handlers.{mod.name}")
        for _, cls in inspect.getmembers(m, inspect.isclass):
            spec = cls.__dict__.get("spec")
            if issubclass(cls, Handler) and spec is not None:
                out[spec.kind] = cls
    # Plugin packages register through the ``precis.handlers`` entry point.
    for ep in entry_points(group="precis.handlers"):
        cls = ep.load()
        if inspect.isclass(cls) and issubclass(cls, Handler):
            out[cls.spec.kind] = cls
    return out


_KIND_CLASS = _load_kind_classes()


@pytest.mark.parametrize(("kind", "verb", "key"), _declared_cases())
def test_declared_args_key_not_rejected(
    runtime_with_store: PrecisRuntime, kind: str, verb: str, key: str
) -> None:
    if runtime_with_store.hub.handler_for(kind) is None:
        # Disabled kinds answer Unsupported, which would pass vacuously.
        pytest.skip(f"kind {kind!r} disabled in this build")
    out = _call(runtime_with_store, kind, verb, {key: 1})
    assert f"not accepted by {kind}.{verb}" not in out, out


def test_every_args_passthrough_verb_declares_its_keys() -> None:
    """A new ``args: dict`` verb without ``ARGS_KEYS`` fails here, loudly."""
    found = []
    for kind, cls in _KIND_CLASS.items():
        for verb in ("get", "put", "edit", "search", "delete"):
            fn = cls.__dict__.get(verb)
            if fn is None or "args" not in inspect.signature(fn).parameters:
                continue
            found.append((kind, verb))
            assert verb in cls.ARGS_KEYS, (
                f"{cls.__name__}.{verb} declares args but ARGS_KEYS lacks it"
            )
    assert sorted(found) == sorted(PASSTHROUGH)


def test_plugin_handlers_are_discovered() -> None:
    """Guard: the entry-point scan must actually see the plugin kinds, or
    the declaration check above passes vacuously for them."""
    assert {"estimate", "pathway", "se"} <= set(_KIND_CLASS)


def test_se_get_keys_are_union_of_view_args() -> None:
    from precis_se.handler import _GLOBAL_ARGS, _VIEW_ARGS, SeHandler

    # Every view's own inputs plus the display knobs every view accepts
    # (``prefunits``) — the same two sets ``_vet_view_args`` unions.
    union: set[str] = set().union(*_VIEW_ARGS.values()) | set(_GLOBAL_ARGS)
    assert SeHandler.ARGS_KEYS["get"] == union


def _se_dispatched_views() -> set[str]:
    """The view names ``SeHandler.get`` actually dispatches, read off the
    AST — every ``v == '…'`` / ``v in (…)`` / ``completion_view`` compare
    in ``_get_impl`` (``get`` is a display-preference shell that forwards
    to it). Static, so a new dispatch branch that forgets the tables
    fails the agreement test below instead of silently dropping its args
    (gr514467)."""
    from precis_se.handler import SeHandler

    node = _fn_node(SeHandler._get_impl)
    assert node is not None
    out: set[str] = set()
    for child in ast.walk(node):
        if not (
            isinstance(child, ast.Compare)
            and isinstance(child.left, ast.Name)
            and child.left.id in ("v", "completion_view")
        ):
            continue
        for op, comp in zip(child.ops, child.comparators, strict=True):
            if (
                isinstance(op, ast.Eq)
                and isinstance(comp, ast.Constant)
                and isinstance(comp.value, str)
            ):
                out.add(comp.value)
            elif isinstance(op, ast.In) and isinstance(comp, ast.Tuple | ast.List):
                out.update(
                    e.value
                    for e in comp.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)
                )
    return out


def test_se_view_tables_agree_with_dispatch() -> None:
    """gr514467 (+ merged gr514468): the three places that describe the
    ``get(kind='se')`` view vocabulary — the dispatch itself,
    :data:`_VIEW_ARGS` (the args-vetting table) and
    ``SeHandler.spec.views`` (the advertised list) — had drifted:
    ``view='order'`` was dispatched but unlisted, so its ``args`` were
    silently dropped, and ~8 dispatched views were missing from
    ``spec.views``. ``_VIEWS_ALL`` (the unknown-view hint's roster) is
    held to the same set."""
    from precis_se.handler import _VIEW_ARGS, _VIEWS_ALL, SeHandler

    dispatched = _se_dispatched_views()
    assert dispatched == set(_VIEW_ARGS)
    advertised = dispatched - {""}
    assert advertised == set(SeHandler.spec.views)
    assert advertised == set(_VIEWS_ALL)


def test_acquire_kwargs_in_args_reach_paper_put(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr484234: ``args={'doi': …, 'verify': False}`` reaches
    ``paper.put`` — the acquire knobs are declared ``ARGS_KEYS`` and the
    handler maps them out of ``args=`` itself (cc1b88ffc). Genuinely
    unknown keys are still rejected."""
    from precis.handlers import paper as paper_mod
    from precis.ingest.semantic_scholar import S2ResolverError

    def _down(*_a: object) -> None:
        raise S2ResolverError("offline in test")

    monkeypatch.setattr(paper_mod, "_lookup_acquire_metadata", _down)

    out = _call(
        runtime_with_store, "paper", "put", {"doi": "10.5555/acq-x", "verify": False}
    )
    assert "[error:BadInput]" not in out, out
    assert "minted stub" in out, out

    bad = _call(
        runtime_with_store, "paper", "put", {"doi": "10.5555/acq-y", "zz_nope": 1}
    )
    assert "not accepted by paper.put" in bad, bad
    assert "zz_nope" in bad


def test_args_key_naming_handler_kwarg_is_promoted_not_rejected(
    runtime_with_store: PrecisRuntime, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr484234: an ``args=`` key naming a real handler kwarg is a misfiled
    top-level argument — dispatch promotes it instead of dying on the
    ``ARGS_KEYS`` gate. ``tags`` exercises it on paper.put: a real kwarg
    the ``ARGS_KEYS`` spec does not declare (the acquire knobs — ``doi``,
    ``verify`` — are declared since cc1b88ffc, so they ride ``args=`` as
    designed, covered above). A genuinely unknown key is still rejected."""
    from precis.handlers import paper as paper_mod
    from precis.ingest.semantic_scholar import S2ResolverError

    def _down(*_a: object) -> None:
        raise S2ResolverError("offline in test")

    monkeypatch.setattr(paper_mod, "_lookup_acquire_metadata", _down)

    out = _call(
        runtime_with_store,
        "paper",
        "put",
        {"doi": "10.5555/acq-x", "verify": False, "tags": ["promotion-probe"]},
    )
    assert "[error:BadInput]" not in out, out
    assert "minted stub" in out, out
    rid = int(out.split("id=", 1)[1].split()[0])
    assert "promotion-probe" in {str(t) for t in store.tags_for(rid)}

    bad = _call(
        runtime_with_store, "paper", "put", {"doi": "10.5555/acq-y", "zz_nope": 1}
    )
    assert "not accepted by paper.put" in bad, bad
    assert "zz_nope" in bad


@pytest.mark.parametrize("kind", ["gripe", "todo"])
def test_unknown_put_args_key_points_at_comment_idiom(
    runtime_with_store: PrecisRuntime, kind: str
) -> None:
    """gr478557: ``args={'comment': True}`` on put names the real comment door."""
    out = runtime_with_store.dispatch(
        "put", {"kind": kind, "id": 1, "text": "x", "__extras__": {"comment": True}}
    )
    assert "[error:BadInput]" in out, out
    assert f"to comment: put(kind='{kind}', id=N, text='...') with no args" in out, out


@pytest.mark.parametrize(
    ("kind", "verb", "params"),
    [
        ("memory", "put", {"text": "x"}),
        ("anki", "put", {"text": "x"}),
        ("gripe", "search", {"q": "x"}),
        ("gripe", "delete", {"id": 1}),
    ],
)
def test_unknown_args_key_no_comment_hint_off_comment_door(
    runtime_with_store: PrecisRuntime, kind: str, verb: str, params: dict
) -> None:
    """The 'to comment:' hint is only for put on a supports_comment kind."""
    out = runtime_with_store.dispatch(
        verb, {"kind": kind, **params, "__extras__": {"bogus_key": True}}
    )
    assert "[error:BadInput]" in out, out
    assert "to comment:" not in out, out


def test_args_keys_derive_from_args_spec() -> None:
    """Every ``ARGS_KEYS`` is built from the class's ``ARGS`` specs; none is
    a hand-written frozenset that can drift from them."""
    for kind, cls in _KIND_CLASS.items():
        if "ARGS" in cls.__dict__:
            assert {v: s.keys for v, s in cls.ARGS.items()} == cls.ARGS_KEYS, kind
        else:
            assert "ARGS_KEYS" not in cls.__dict__, f"{kind}: ARGS_KEYS without ARGS"


def test_args_view_rejects_undeclared_reads() -> None:
    view = ArgsSpec({"a": int, "b": str}).view({"a": 1}, "x.get args=")
    assert view["a"] == 1
    assert view.get("b") is None
    assert "b" not in view
    assert dict(view) == {"a": 1}
    for read in (
        lambda: view["c"],
        lambda: view.get("c"),
        lambda: "c" in view,
        lambda: view.pop("c", None),
        lambda: view.setdefault("c", 1),
    ):
        with pytest.raises(UndeclaredArgsKey):
            read()


def _literal_args_reads(fn: ast.AST, name: str = "args") -> set[str]:
    """String literals a function body reads from its ``name`` parameter."""
    out: set[str] = set()
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("get", "pop")
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == name
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            out.add(node.args[0].value)
        elif (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id == name
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            out.add(node.slice.value)
        elif (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Constant)
            and isinstance(node.left.value, str)
            and any(isinstance(op, ast.In | ast.NotIn) for op in node.ops)
            and any(isinstance(c, ast.Name) and c.id == name for c in node.comparators)
        ):
            out.add(node.left.value)
    return out


def _fn_node(fn: Callable[..., Any]) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(inspect.unwrap(fn))))
    except (OSError, TypeError):
        return None
    node = tree.body[0]
    return node if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) else None


def _reads_following_helpers(
    cls: type[Handler], fn: Callable[..., Any], name: str, seen: set[object]
) -> set[str]:
    """Literal keys read from ``name`` in ``fn`` and in every ``self.m(...)``
    / module-level helper it hands the whole dict to (``op='order'`` lives in
    ``_op_order(ref, args)``, not in ``put`` itself)."""
    node = _fn_node(fn)
    if node is None or (fn, name) in seen:
        return set()
    seen.add((fn, name))
    out = _literal_args_reads(node, name)
    module = inspect.getmodule(fn)
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        callee: object = None
        offset = 0
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in ("self", "cls")
        ):
            callee = inspect.getattr_static(cls, func.attr, None)
            offset = 0 if isinstance(callee, staticmethod) else 1
            callee = getattr(callee, "__func__", callee)
        elif isinstance(func, ast.Name) and module is not None:
            callee = getattr(module, func.id, None)
        if not inspect.isfunction(callee):
            continue
        params = list(inspect.signature(callee).parameters)
        passed: list[str] = []
        for i, a in enumerate(call.args):
            if isinstance(a, ast.Name) and a.id == name and i + offset < len(params):
                passed.append(params[i + offset])
        passed += [
            kw.arg
            for kw in call.keywords
            if kw.arg and isinstance(kw.value, ast.Name) and kw.value.id == name
        ]
        for p in passed:
            out |= _reads_following_helpers(cls, callee, p, seen)
    return out


@pytest.mark.parametrize(("kind", "verb"), PASSTHROUGH)
def test_verb_body_reads_only_declared_keys(kind: str, verb: str) -> None:
    """A literal ``args.get('k')`` / ``args['k']`` / ``'k' in args`` in the
    verb body, or in a helper the verb passes the whole dict to, must be a
    declared key (the static half of ``ArgsView``)."""
    cls = _KIND_CLASS[kind]
    reads = _reads_following_helpers(cls, cls.__dict__[verb], "args", set())
    undeclared = reads - cls.ARGS_KEYS[verb]
    assert not undeclared, f"{kind}.{verb} reads undeclared args keys {undeclared}"
