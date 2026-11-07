"""Git-worktree roots for the md kind (PRECIS_PYTHON_WORKTREES, shared spec).

Mirrors ``tests/test_python_worktree_roots.py``: the same
``WorktreeRegistry`` discovery, the same ``wt-<tree>`` aliases, the
same read-only semantics — over markdown instead of code.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from precis.dispatch import Hub
from precis.errors import NotFound, Unsupported
from precis.handlers._python_worktrees import WorktreeRegistry
from precis.handlers.md import MdHandler

HOST = "/hostprefix"


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _checkout(
    path: Path, body: str = "# Worktree notes\n\nworktree-only prose\n"
) -> None:
    path.mkdir(parents=True)
    (path / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    (path / "doc.md").write_text(body, encoding="utf-8")


def _register(main: Path, ident: str, host_path: str) -> None:
    d = main / ".git" / "worktrees" / ident
    d.mkdir(parents=True)
    (d / "gitdir").write_text(f"{host_path}/.git\n", encoding="utf-8")


@pytest.fixture
def env(tmp_path: Path):
    main = tmp_path / "main"
    (main / ".git").mkdir(parents=True)
    (main / "doc.md").write_text("# Main doc\n\nlanded prose\n", encoding="utf-8")
    wts = tmp_path / "wts"
    return main, wts, tmp_path


def _make(env, names=("alpha",), **kw):
    main, wts, tmp = env
    for n in names:
        _checkout(wts / n)
        _register(main, n, f"{HOST}/wts/{n}")
    clock = Clock()
    reg = WorktreeRegistry(
        "wt",
        main,
        gitdir_map=(HOST, str(tmp)),
        clock=clock,
        **kw,
    )
    h = MdHandler(hub=Hub(), roots={"main": main}, worktrees=reg)
    return h, reg, clock


def test_discovery_and_resolution(env):
    h, reg, _ = _make(env, ("alpha", "chat"))
    assert set(reg.listing()[0]) == {"wt-alpha", "wt-chat"}
    overview = h.get(id="wt-alpha").body
    assert "doc.md" in overview
    outline = h.get(id="wt-alpha/doc.md").body
    assert "Worktree notes" in outline
    source = h.get(id="wt-alpha/doc.md", view="source").body
    assert "worktree-only prose" in source
    section = h.get(id="wt-alpha/doc.md~Worktree notes").body
    assert "worktree-only prose" in section


def test_unknown_worktree_refusal_with_next(env):
    h, _, _ = _make(env)
    with pytest.raises(NotFound, match="not configured") as exc:
        h.get(id="wt-ghost/doc.md")
    assert exc.value.next is not None
    assert "get(kind='md')" in str(exc.value.next)
    assert exc.value.options is not None
    assert "wt-alpha" in exc.value.options


def test_unknown_non_worktree_alias_unchanged(env):
    h, _, _ = _make(env)
    with pytest.raises(NotFound, match="unknown md root alias") as exc:
        h.get(id="nope/doc.md")
    assert exc.value.next is not None


def test_writes_refused(env):
    h, _, _ = _make(env)
    with pytest.raises(Unsupported):
        h.put(id="wt-alpha/doc.md", mode="create", text="x\n")
    with pytest.raises(Unsupported):
        h.edit(id="wt-alpha/doc.md", mode="append", text="x\n")
    with pytest.raises(Unsupported):
        h.delete(id="wt-alpha/doc.md")


def test_static_wins_on_collision(env):
    main, wts, tmp = env
    h, reg, _ = _make(env, ("alpha",))
    static = tmp / "static"
    static.mkdir()
    (static / "doc.md").write_text("# Static\n\nstatic prose\n", encoding="utf-8")
    h2 = MdHandler(hub=Hub(), roots={"wt-alpha": static}, worktrees=reg)
    out = h2.get(id="wt-alpha/doc.md").body
    assert "Static" in out


def test_disappearance_drops_cache_and_resolution(env):
    h, reg, clock = _make(env)
    h.get(id="wt-alpha/doc.md")
    path = reg.listing()[0]["wt-alpha"].path
    assert path in h.cache.known_roots()
    (path / ".git").unlink()
    # within the throttle window it still resolves
    assert h._resolve_alias("wt-alpha") == path
    clock.t += 6
    with pytest.raises(NotFound, match="not configured"):
        h._resolve_alias("wt-alpha")
    assert path not in h.cache.known_roots()


def test_throttle_picks_up_new_worktree(env):
    main, wts, tmp = env
    h, reg, clock = _make(env)
    h._resolve_alias("wt-alpha")  # primes the listing
    _checkout(wts / "beta")
    _register(main, "beta", f"{HOST}/wts/beta")
    with pytest.raises(NotFound):
        h._resolve_alias("wt-beta")
    clock.t += 6
    assert h._resolve_alias("wt-beta").name == "beta"


def test_not_in_default_fanout(env):
    h, _, _ = _make(env)
    default = h.search(q="worktree-only prose").body
    assert "wt-alpha" not in default
    named = h.search(q="worktree-only prose", scope="wt-alpha").body
    assert "wt-alpha" in named


def test_absolute_worktree_path_maps_to_wt_alias(env):
    """gr572707: an absolute path inside a discovered worktree reads as
    its wt-<tree> alias id instead of refusing 'missing alias'."""
    h, reg, _ = _make(env)
    wt_path = reg.listing()[0]["wt-alpha"].path
    resp = h.get(id=str(wt_path / "doc.md"))
    assert resp.body.startswith("interpreted:")
    assert "read as 'wt-alpha/doc.md'" in resp.body
    assert "Worktree notes" in resp.body


def test_absolute_host_path_maps_via_gitdir_map(env):
    """The agent's host-side spelling translates through
    PRECIS_PYTHON_GITDIR_MAP to the container path the registry knows."""
    h, _, _ = _make(env)
    resp = h.get(id=f"{HOST}/wts/alpha/doc.md~Worktree notes")
    assert "worktree-only prose" in resp.body
    assert "read as 'wt-alpha/doc.md~Worktree notes'" in resp.body


def test_missing_file_names_worktree_holding_it(env):
    """A file absent from main but present in a worktree checkout is
    named in the miss (the lagging-checkout shape, gr572707)."""
    h, reg, _ = _make(env)
    wt_path = reg.listing()[0]["wt-alpha"].path
    (wt_path / "extra.md").write_text("# Extra\n\nwt only\n", encoding="utf-8")
    with pytest.raises(NotFound, match="not found") as exc:
        h.get(id="main/extra.md")
    nxt = exc.value.next
    assert isinstance(nxt, list)
    assert "get(kind='md', id='wt-alpha/extra.md')" in nxt


def test_no_registry_behaves_as_before(tmp_path: Path):
    """Without a WorktreeRegistry nothing changes: wt-* is just an
    unknown alias."""
    (tmp_path / "doc.md").write_text("# Doc\n\ntext\n", encoding="utf-8")
    h = MdHandler(hub=Hub(), roots={"main": tmp_path})
    with pytest.raises(NotFound, match="unknown md root alias"):
        h.get(id="wt-anything/doc.md")
