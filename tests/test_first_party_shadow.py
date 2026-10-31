"""gr524633: a copied first-party package in ``site-packages`` must never
shadow the vendored ``src/`` copy this venv edits.

``seed-worktree-venv`` clones a sibling ``.venv`` wholesale, so one stale
``site-packages/hexfold`` in the source venv lands in every fresh tree —
and site-packages sits ahead of the editable ``.pth``'s ``src`` entry on
``sys.path``, so ``import hexfold`` silently ran an old wheel that
predates ``hexfold.wrap``. The seed now strips src-shadowing copies;
this test is the belt: it checks the venv the suite actually runs in,
so a shadow that arrives any other way fails loudly too.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import tomllib
from pathlib import Path

import pytest

import hexfold

_REPO = Path(__file__).resolve().parent.parent
_SRC = (_REPO / "src").resolve()


def _under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _editable_install() -> bool:
    """precis-mcp installed editable — or not installed at all (a bare
    src checkout on ``sys.path`` is the same shape for shadow purposes)."""
    try:
        dist = importlib.metadata.distribution("precis-mcp")
    except importlib.metadata.PackageNotFoundError:
        return True
    try:
        direct_url = json.loads(dist.read_text("direct_url.json") or "{}")
    except (OSError, json.JSONDecodeError):
        return False
    return bool(direct_url.get("dir_info", {}).get("editable"))


def _declared_in_lock(name: str) -> bool:
    """``name`` is a resolved distribution in ``uv.lock`` — a real
    dependency, not a vendored ``src/`` package."""
    want = name.replace("_", "-").lower()
    try:
        data = tomllib.loads((_REPO / "uv.lock").read_text(encoding="utf-8"))
    except OSError:
        return False
    return any(
        str(pkg.get("name", "")).replace("_", "-").lower() == want
        for pkg in data.get("package", [])
    )


def _assert_no_shadow(name: str, origin: Path | None) -> None:
    assert origin is not None, f"{name} is not importable in this venv"
    if _under(origin, _SRC):
        return
    if _declared_in_lock(name) or not _editable_install():
        return  # a locked dep, or a wheel install: site-packages is home
    raise AssertionError(
        f"{name} resolves to {origin}, not under {_SRC / name} — a copied "
        "site-packages package shadows the tree's src/ copy (gr524633: a "
        "stale wheel rode a seeded .venv). Fix the venv: delete the copy "
        f"(rm -rf .venv/lib/python*/site-packages/{name}*) and `uv sync`, "
        "or rebuild it: rm -rf .venv && uv sync."
    )


def test_hexfold_resolves_under_repo_src() -> None:
    """``import hexfold`` must land on ``src/hexfold`` — the shape that
    broke cad's wrap import when a stale wheel shadowed it."""
    assert hexfold.__file__ is not None
    _assert_no_shadow("hexfold", Path(hexfold.__file__).resolve())


def test_hexfold_shadow_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard fires on the observed failure shape, not only passes on
    the healthy one."""
    if not _editable_install() or _declared_in_lock("hexfold"):
        pytest.skip("site-packages is a legitimate home for hexfold here")
    monkeypatch.setattr(
        hexfold,
        "__file__",
        "/x/.venv/lib/python3.13/site-packages/hexfold/__init__.py",
    )
    assert hexfold.__file__ is not None
    with pytest.raises(AssertionError, match="shadows the tree"):
        _assert_no_shadow("hexfold", Path(hexfold.__file__).resolve())


def test_no_first_party_package_is_shadowed() -> None:
    """Every top-level ``src/`` package resolves under ``src/`` — hexfold
    was the observed copy, but a seeded venv can carry any of them."""
    if not _editable_install():
        pytest.skip("non-editable install: site-packages is the home")
    names = sorted(p.name for p in _SRC.iterdir() if (p / "__init__.py").is_file())
    assert names
    for name in names:
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, ValueError):
            spec = None
        origin = Path(spec.origin).resolve() if spec and spec.origin else None
        _assert_no_shadow(name, origin)
