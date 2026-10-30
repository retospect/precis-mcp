"""Git-worktree roots for the python kind (PRECIS_PYTHON_WORKTREES)."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers._python_provenance import WORKTREE_NOTE
from precis.handlers._python_worktrees import (
    WorktreeRegistry,
    parse_gitdir_map,
    parse_worktrees_spec,
)
from precis.handlers.python import PythonHandler

_POSIX_GITFILE_WRITE = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only: Windows refuses writing the linked worktree's read-only .git pointer file (PermissionError)",
)

HOST = "/hostprefix"


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _checkout(path: Path, body: str = "def f():\n    return 1\n") -> None:
    path.mkdir(parents=True)
    (path / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    (path / "m.py").write_text(body, encoding="utf-8")


def _register(main: Path, ident: str, host_path: str) -> None:
    d = main / ".git" / "worktrees" / ident
    d.mkdir(parents=True)
    (d / "gitdir").write_text(f"{host_path}/.git\n", encoding="utf-8")


@pytest.fixture
def env(tmp_path: Path):
    main = tmp_path / "main"
    (main / ".git").mkdir(parents=True)
    (main / "m.py").write_text("def f():\n    return 0\n", encoding="utf-8")
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
    h = PythonHandler(hub=Hub(), roots={"main": main}, worktrees=reg)
    return h, reg, clock


def test_discovery_and_alias(env):
    h, reg, _ = _make(env, ("alpha", "codex-foo"))
    assert set(reg.listing()[0]) == {"wt-alpha", "wt-codex-foo"}
    out = h.get(id="wt-alpha::m.f").body
    assert "return 1" in out or "def f" in out


@pytest.mark.parametrize("verb", ["get", "search", "search-no-hits"])
def test_matching_dynamic_worktree_reads_have_no_missing_index_note(env, verb):
    h, _, _ = _make(env)
    if verb == "get":
        body = h.get(id="wt-alpha::m.f").body
    else:
        body = h.search(
            q="f" if verb == "search" else "never_present",
            scope="wt-alpha",
            mode="lexical",
        ).body
    assert "checkout: wt-alpha@" in body
    assert WORKTREE_NOTE not in body


@pytest.mark.parametrize("verb", ["get", "search"])
def test_missing_worktree_alias_warns_even_with_another_index(env, verb):
    h, _, _ = _make(env)
    h.get(id="wt-alpha::m.f")
    with pytest.raises(NotFound) as caught:
        if verb == "get":
            h.get(id="wt-missing::m.f")
        else:
            h.search(q="f", scope="wt-missing", mode="lexical")
    error = caught.value
    assert error.cause.count(WORKTREE_NOTE) == 1
    if verb == "get":
        assert "worktree 'wt-missing' not configured" in error.cause
        assert error.options == ["main", "wt-alpha"]
        assert (
            error.next
            == "Grep/Read in your own tree, or get(kind='python') to list roots"
        )
    else:
        assert "no python repo matches scope='wt-missing'" in error.cause
        assert error.options is None
        assert error.next == "search(kind='python', q='...') to search all repos"


@pytest.mark.parametrize("verb", ["get", "search"])
def test_registered_but_cold_worktree_note_does_not_scan_or_warm(
    env, monkeypatch: pytest.MonkeyPatch, verb
):
    main, wts, _ = env
    h, reg, _ = _make(env)
    h._resolve_alias("wt-alpha")  # Registered, not indexed.

    def forbidden(*args, **kwargs):
        raise AssertionError("the note must not discover or warm worktrees")

    monkeypatch.setattr(reg, "listing", forbidden)
    monkeypatch.setattr(h.cache, "warm", forbidden)
    if verb == "get":
        body = h.get(id="main::m.f").body
    else:
        body = h.search(q="f", scope="main", mode="lexical").body
    assert WORKTREE_NOTE not in body
    assert "checkout: main@" in body
    assert h.cache.known_roots() == [main.resolve()]
    assert (wts / "alpha").resolve() not in h.cache.known_roots()
    assert not h._wt_warm_inflight


@pytest.mark.parametrize("verb", ["get", "search"])
def test_empty_worktree_registry_warns_without_scanning_or_warming(
    env, monkeypatch: pytest.MonkeyPatch, verb
):
    h, reg, _ = _make(env, ())

    def forbidden(*args, **kwargs):
        raise AssertionError("the note must not discover or warm worktrees")

    monkeypatch.setattr(reg, "listing", forbidden)
    monkeypatch.setattr(h.cache, "warm", forbidden)
    if verb == "get":
        body = h.get(id="main::m.f").body
    else:
        body = h.search(q="f", scope="main", mode="lexical").body
    assert body.count(WORKTREE_NOTE) == 1
    assert not h._wt_warm_inflight


def test_missing_symbol_in_registered_worktree_does_not_warn(env):
    h, _, _ = _make(env)
    with pytest.raises(NotFound) as caught:
        h.get(id="wt-alpha::m.missing")
    assert WORKTREE_NOTE not in caught.value.cause


def test_bad_names_main_and_missing_skipped(env):
    main, wts, tmp = env
    _checkout(wts / "Bad_Name")
    _register(main, "bad", f"{HOST}/wts/Bad_Name")
    _register(main, "gone", f"{HOST}/wts/gone")  # path missing (prunable)
    _register(main, "self", f"{HOST}/main")  # main checkout itself
    _register(main, "outside", "/tmp/elsewhere/tree")  # outside mapped prefix
    h, reg, _ = _make(env, ("ok",))
    assert set(reg.listing()[0]) == {"wt-ok"}


def test_static_wins_on_collision(env):
    main, wts, tmp = env
    h, reg, _ = _make(env, ("alpha",))
    static = tmp / "static"
    static.mkdir()
    (static / "m.py").write_text("def g():\n    return 2\n", encoding="utf-8")
    h2 = PythonHandler(hub=Hub(), roots={"wt-alpha": static}, worktrees=reg)
    assert h2._resolve_alias("wt-alpha") == static.resolve()


def test_disappearance_drops_cache_and_resolution(env):
    h, reg, clock = _make(env)
    h.get(id="wt-alpha::m.f")
    path = reg.listing()[0]["wt-alpha"].path
    assert path in h.cache.known_roots()
    (path / ".git").unlink()
    # within the throttle window it still resolves
    assert h._resolve_alias("wt-alpha") == path
    clock.t += 6
    with pytest.raises(NotFound, match="does not exist"):
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


def test_lru_eviction(env):
    h, reg, _ = _make(env, ("a", "b", "c"), max_indexes=2)
    for n in ("a", "b", "c"):
        h.get(id=f"wt-{n}::m.f")
    known = {p.name for p in h.cache.known_roots()}
    assert "a" not in known and {"b", "c"} <= known
    h.get(id="main::m.f")  # static roots are never evicted
    assert any(p.name == "main" for p in h.cache.known_roots())


def test_idle_expiry_keeps_recent(env):
    h, reg, clock = _make(env, ("a", "b", "c", "d", "e"))
    for n in ("a", "b", "c", "d", "e"):
        h.get(id=f"wt-{n}::m.f")  # no cap by default: all five stay
    assert {"a", "b", "c", "d", "e"} <= {p.name for p in h.cache.known_roots()}
    clock.t += 23 * 3600
    h.get(id="wt-e::m.f")
    clock.t += 2 * 3600  # a-d idle 25 h, e idle 2 h
    h.get(id="main::m.f")
    h._resolve_alias("wt-e")
    known = {p.name for p in h.cache.known_roots()}
    assert "e" in known and not ({"a", "b", "c", "d"} & known)


def _drain_warm(h: PythonHandler) -> None:
    """Wait out any speculative warm threads (bounded, never hangs)."""
    deadline = time.monotonic() + 10
    while h._wt_warm_inflight and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not h._wt_warm_inflight


def test_first_listing_is_baseline_no_warm(env):
    """gr525769 follow-up: the first scan only primes — trees it names
    are never speculatively warmed. Only a tree a *later* listing
    discovers earns a warm."""
    main, wts, _ = env
    h, reg, clock = _make(env, ("alpha",))
    h._resolve_alias("wt-alpha")
    _drain_warm(h)
    alpha = reg.listing()[0]["wt-alpha"].path
    assert alpha not in h.cache.known_roots()  # resolved, not indexed
    # beta appears after the first listing → it warms.
    _checkout(wts / "beta")
    _register(main, "beta", f"{HOST}/wts/beta")
    clock.t += 6
    h._resolve_alias("wt-beta")
    _drain_warm(h)
    beta = reg.listing()[0]["wt-beta"].path
    assert beta in h.cache.known_roots()


def test_background_warm_indexes_new_worktrees(env):
    """gr525769: a scan that discovers a new tree kicks a background
    corpus warm — a later first hit reuses the built index instead of
    paying the cold build."""
    main, wts, _ = env
    h, reg, clock = _make(env, ("alpha",))
    h._resolve_alias("wt-alpha")  # primes the baseline listing
    _checkout(wts / "beta")
    _register(main, "beta", f"{HOST}/wts/beta")
    clock.t += 6
    h._resolve_alias("wt-beta")
    _drain_warm(h)
    beta = reg.listing()[0]["wt-beta"].path
    # beta was warmed in the background — no beta query happened yet.
    assert beta in h.cache.known_roots()
    # Its first real call is a stat-check, not a cold rebuild.
    out = h.get(id="wt-beta::m.f").body
    assert "cold corpus" not in out
    assert "def f" in out or "return 1" in out


def test_warm_max_zero_disables_warming(env, monkeypatch: pytest.MonkeyPatch):
    """``PRECIS_PYTHON_WT_WARM_MAX=0`` turns speculative warming off."""
    monkeypatch.setenv("PRECIS_PYTHON_WT_WARM_MAX", "0")
    main, wts, _ = env
    h, reg, clock = _make(env, ("alpha",))
    h._resolve_alias("wt-alpha")
    _checkout(wts / "beta")
    _register(main, "beta", f"{HOST}/wts/beta")
    clock.t += 6
    h._resolve_alias("wt-beta")
    _drain_warm(h)
    beta = reg.listing()[0]["wt-beta"].path
    assert beta not in h.cache.known_roots()


def test_warm_cap_counts_held_indexes(env, monkeypatch: pytest.MonkeyPatch):
    """The cap bounds warm-built indexes *currently held*, not a
    lifetime total: a full cap skips fresh discoveries, and evicting a
    warmed index frees the slot for the next listing."""
    monkeypatch.setenv("PRECIS_PYTHON_WT_WARM_MAX", "1")
    main, wts, _ = env
    h, reg, clock = _make(env, ("alpha",))
    h._resolve_alias("wt-alpha")  # baseline: primed, no warm
    _checkout(wts / "beta")
    _register(main, "beta", f"{HOST}/wts/beta")
    clock.t += 6
    h._resolve_alias("wt-beta")
    _drain_warm(h)
    beta = reg.listing()[0]["wt-beta"].path
    assert beta in h.cache.known_roots()
    # gamma appears while the cap (1 held) is full — it stays cold.
    _checkout(wts / "gamma")
    _register(main, "gamma", f"{HOST}/wts/gamma")
    clock.t += 6
    h._resolve_alias("wt-gamma")
    _drain_warm(h)
    gamma = reg.listing()[0]["wt-gamma"].path
    assert gamma not in h.cache.known_roots()
    # Free the held warm → the next listing warms gamma.
    h.cache.drop(beta)
    clock.t += 6
    h._resolve_alias("wt-gamma")
    _drain_warm(h)
    assert gamma in h.cache.known_roots()


def test_warm_semantic_does_not_scan_worktrees(env):
    """The boot hook warms symbol embeddings only — worktree corpora
    warm lazily on discovery, never from a boot scan."""
    h, _, _ = _make(env, ("alpha",))
    h._semantic.start_warmup = lambda *a, **k: None
    h.warm_semantic()
    assert not h._wt_warm_primed
    assert not h._wt_warm_inflight
    assert not h.cache.known_roots()


def test_queued_warm_does_not_rebuild_evicted_root(env):
    """A warm queued on the per-root lock when its tree vanished and
    drop() ran must not rebuild — the rebuilt index would sit in the
    cache outside the registry LRU, freed by nothing."""
    h, reg, clock = _make(env, ("alpha",))
    h._resolve_alias("wt-alpha")
    path = reg.listing()[0]["wt-alpha"].path
    # Occupy the root lock so the warm queues behind it.
    lock = h.cache._lock_for(path)
    lock.acquire()
    t = threading.Thread(target=h._warm_one, args=("wt-alpha", path), daemon=True)
    t.start()
    # While the warm waits: the tree vanishes and its drop runs.
    (path / ".git").unlink()
    clock.t += 6
    h._worktree_listing()  # reaps: alpha gone → cache.drop(path)
    lock.release()
    t.join(5)
    assert not t.is_alive()
    assert path not in h.cache.known_roots()


def test_read_only_refusal(env):
    h, _, _ = _make(env)
    with pytest.raises(BadInput, match="read-only worktree"):
        h.edit(id="wt-alpha/m.py", mode="append", text="x = 1\n")
    with pytest.raises(BadInput, match="read-only worktree"):
        h.delete(id="wt-alpha/m.py")


def test_not_in_default_fanout(env):
    h, _, _ = _make(env)
    default = h.search(q="f", mode="lexical").body
    assert "wt-alpha" not in default
    named = h.search(q="f", scope="wt-alpha", mode="lexical").body
    assert "wt-alpha" in named


def test_gitdir_and_parse_helpers(env):
    main, wts, tmp = env
    h, reg, _ = _make(env)
    root = reg.listing()[0]["wt-alpha"].path
    assert h.git_dir_for(root) == main / ".git" / "worktrees" / "alpha"
    assert parse_gitdir_map("/a/b:/main") == ("/a/b", "/main")
    assert parse_gitdir_map(None) is None
    assert parse_worktrees_spec(f"wt:{main}") == ("wt", main.resolve())
    assert parse_worktrees_spec("wt:/nonexistent") is None


@_POSIX_GITFILE_WRITE
def test_provenance_uses_gitdir(env):
    main, wts, tmp = env
    # Real linked worktree whose .git file points at a host path we remap.
    subprocess.run(["git", "init", "-q", str(main)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(main),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "i",
        ],
        check=True,
    )
    wt = wts / "real"
    wts.mkdir()
    subprocess.run(
        ["git", "-C", str(main), "worktree", "add", "-q", str(wt)], check=True
    )
    (wt / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    reg = WorktreeRegistry("wt", main, gitdir_map=(str(tmp), str(tmp)))
    # Break the worktree's own .git pointer, as in a container.
    (wt / ".git").write_text("gitdir: /nonexistent/host/path\n", encoding="utf-8")
    h = PythonHandler(hub=Hub(), roots={"main": main}, worktrees=reg)
    body = h.get(id="wt-real::m.f").body
    assert "git UNAVAILABLE" not in body
    assert "git " in body


def test_held_worktree_symbols_only_non_static_roots(env):
    h, reg, _ = _make(env)
    assert h._held_worktree_symbols() == []  # nothing cached yet
    h.get(id="wt-alpha::m.f")
    h.get(id="main::m.f")
    syms = h._held_worktree_symbols()
    assert syms and all(s.file == "m.py" for s in syms)
    wt_root = reg.listing()[0]["wt-alpha"].path
    assert len(syms) == sum(len(m.symbols) for m in h.cache.held_modules(wt_root))
    assert h.cache.held_modules(wt_root / "nope") == []


def test_warm_semantic_passes_keep_callback(env):
    h, _, _ = _make(env)
    seen = {}

    def fake(symbols, *, keep=None, block=False):
        seen["symbols"], seen["keep"] = symbols, keep

    h._semantic.start_warmup = fake
    h.warm_semantic()
    assert seen["symbols"] == h._all_symbols
    assert seen["keep"] == h._held_worktree_symbols
