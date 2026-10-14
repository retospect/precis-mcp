"""gr481617: every sort a handler suggests passes the surface sort check."""

import inspect
import re

import pytest

from precis.runtime import PrecisRuntime
from precis.tools import core
from precis.utils.browse_sort import numeric_browse

# ``search(kind='cfp', args={'sort': 'deadline'})`` / ``search(kind='x', sort='y')``
_HINT = re.compile(
    r"search\(kind=['\"](\w+)['\"][^)]*?sort['\"]?\s*[:=]\s*['\"]([a-z_]+)['\"]"
)


def _accepts(kind: str, sort: str) -> bool:
    return sort in core._accepted_sorts(kind, numeric_browse(kind, {"q": None}))


def test_handler_suggested_sorts_pass_surface_check(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(core, "_get_runtime", lambda: runtime_with_store)
    hub = runtime_with_store.hub
    checked = set()
    for kind, handler in hub.handlers.items():
        for cls in type(handler).__mro__:
            if cls is object:
                continue
            module = inspect.getmodule(cls)
            if module is None:
                continue
            for k, sort in _HINT.findall(inspect.getsource(module)):
                if k == kind:
                    checked.add((k, sort))
        for sort in getattr(handler, "EXTRA_SORTS", ()):
            checked.add((kind, sort))
    assert ("cfp", "deadline") in checked
    bad = sorted(c for c in checked if not _accepts(*c))
    assert not bad, f"hints/declared sorts rejected by the surface check: {bad}"


def test_cfp_explicit_deadline_sort_not_rejected(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(core, "_get_runtime", lambda: runtime_with_store)
    out = core.search(kind="cfp", sort="deadline")
    text = out if isinstance(out, str) else "\n".join(c.text for c in out.content)
    assert "unknown search sort" not in text


def test_rejected_sort_names_the_kinds_accepted_sorts(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bad-sort hint must enumerate the kind's actual vocabulary —
    including handler-owned EXTRA_SORTS — not just the paper recipe."""
    monkeypatch.setattr(core, "_get_runtime", lambda: runtime_with_store)
    out = core.search(kind="cfp", sort="bogus")
    text = out if isinstance(out, str) else "\n".join(c.text for c in out.content)
    assert "unknown search sort" in text
    assert "deadline" in text
