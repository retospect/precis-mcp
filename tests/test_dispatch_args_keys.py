"""gr475332: unknown ``args=`` keys are rejected on passthrough handlers.

A handler whose verb method declares ``args: dict`` receives the whole
extras dict instead of flattened kwargs, which used to skip the
unknown-key gate (``todo.search(args={'bogus': 1})`` silently dropped
``bogus``). Such handlers now declare ``ARGS_KEYS[verb]``; dispatch
rejects anything else with the same ``BadInput`` shape as paper.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from importlib.metadata import entry_points

import pytest

import precis.handlers as handlers_pkg
from precis.protocol import Handler
from precis.runtime import PrecisRuntime

#: (kind, verb) for every in-repo handler verb that declares ``args``.
PASSTHROUGH = [
    ("todo", "search"),
    ("draft", "get"),
    ("pcb", "put"),
    ("pcb", "get"),
    ("cad", "get"),
    ("component", "put"),
    ("component", "get"),
    ("quest", "get"),
    ("structure", "put"),
    ("structure", "edit"),
    ("structure", "get"),
    ("random", "get"),
    ("time", "get"),
    # Plugin handlers (entry point group ``precis.handlers``).
    ("estimate", "get"),
    ("pathway", "get"),
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
    from precis_se.handler import _VIEW_ARGS, SeHandler

    union: set[str] = set().union(*_VIEW_ARGS.values())
    assert SeHandler.ARGS_KEYS["get"] == union
