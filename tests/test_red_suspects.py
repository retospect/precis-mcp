"""``precis.plans.red_suspects``: the ranked changed-files table the
red build's brief carries (gr595552) — the git reads on a throwaway
repo, the ledger map on canned rows, the pure ranking and rendering."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from precis.handlers._python_impact import ChangedSymbol
from precis.plans import red_suspects as S


def _git(root: Path, *args: str) -> str:
    cp = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return cp.stdout.strip()


def _commit(root: Path, files: dict[str, str], msg: str) -> str:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    _git(root, "add", "-A")
    _git(
        root,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.invalid",
        "commit",
        "-q",
        "-m",
        msg,
    )
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    return root


# ── the git reads ──────────────────────────────────────────────────


def test_changed_files_groups_stat_and_commits(repo: Path) -> None:
    base = _commit(
        repo,
        {
            "src/precis/widgets.py": "A = 1\n",
            "tests/test_widgets.py": "def test_a(): ...\n",
        },
        "base",
    )
    one = _commit(repo, {"src/precis/widgets.py": "A = 2\nB = 3\n"}, "one")
    tip = _commit(
        repo,
        {"src/precis/widgets.py": "A = 2\nB = 3\nC = 4\n", "docs/x.md": "hi\n"},
        "two",
    )
    rng = f"{base}..{tip}"
    files = {f.path: f for f in S.changed_files(repo, rng) or []}
    assert set(files) == {"src/precis/widgets.py", "docs/x.md"}
    w = files["src/precis/widgets.py"]
    assert (w.added, w.deleted) == (3, 1)  # B and C added, A rewritten
    assert w.commits == (tip, one)  # newest first, both commits touched it
    d = files["docs/x.md"]
    assert (d.added, d.deleted) == (1, 0)
    assert d.commits == (tip,)


def test_changed_files_refuses_an_unreadable_range(repo: Path) -> None:
    _commit(repo, {"a.txt": "x\n"}, "base")
    assert S.changed_files(repo, "deadbeef") is None
    assert S.changed_files(repo, "aaaa1111..bbbb2222") is None
    assert S.changed_files(repo.parent / "nope", "a..b") is None


def test_binary_files_count_zero(repo: Path) -> None:
    base = _commit(repo, {"a.txt": "x\n"}, "base")
    tip = _commit(repo, {"blob.bin": "\x00\x01\x02"}, "add blob")
    files = {f.path: f for f in S.changed_files(repo, f"{base}..{tip}") or []}
    assert files["blob.bin"].added == 0 and files["blob.bin"].deleted == 0


# ── the last green build ───────────────────────────────────────────


def test_last_green_skips_waived_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        {"sha": "a" * 12, "verdict": "green", "at": "2026-01-01T00:00:00Z"},
        {
            "sha": "b" * 12,
            "verdict": "green",
            "at": "2026-01-02T00:00:00Z",
            "waived": {"gripe": 1},
        },
        {"sha": "c" * 12, "verdict": "red", "at": "2026-01-03T00:00:00Z"},
    ]
    monkeypatch.setattr(S._gates, "read_ci_verdicts", lambda store: rows)
    # The waived "green" was a red run deployed past — the real last
    # green is the older row.
    assert S.last_green_sha(store=None) == "a" * 12


def test_last_green_unreadable_ledger_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(store: Any) -> list[dict[str, Any]]:
        raise RuntimeError("db down")

    monkeypatch.setattr(S._gates, "read_ci_verdicts", boom)
    assert S.last_green_sha(store=None) is None


def test_suspect_range_prefers_last_green_falls_back_to_the_cut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        S._gates,
        "read_ci_verdicts",
        lambda store: [
            {"sha": "a" * 12, "verdict": "green", "at": "2026-01-01T00:00:00Z"},
        ],
    )
    assert S.suspect_range(None, "c" * 12, f"{'b' * 12}..{'c' * 12}") == (
        f"{'a' * 12}..{'c' * 12}"
    )
    monkeypatch.setattr(S._gates, "read_ci_verdicts", lambda store: [])
    assert S.suspect_range(None, "", f"{'b' * 12}..{'c' * 12}") == (
        f"{'b' * 12}..{'c' * 12}"
    )
    assert S.suspect_range(None, "", "deadbeef") is None


# ── the commit → ledger map ────────────────────────────────────────


class _FakeStore:
    """``store.pool.connection()`` over canned rows for the ledger reads."""

    def __init__(
        self,
        comments: list[tuple[str, int, str]] | None = None,
        plan_tags: list[tuple[int, str]] | None = None,
        links: list[tuple[int, int]] | None = None,
    ) -> None:
        self._comments = comments or []
        self._plan_tags = plan_tags or []
        self._links = links or []
        self._rows: list[Any] = []

    @property
    def pool(self) -> _FakeStore:
        return self

    def connection(self) -> _FakeStore:
        return self

    def __enter__(self) -> _FakeStore:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> _FakeStore:
        if "chunk_kind" in sql:
            self._rows = list(self._comments)
        elif "t.value LIKE" in sql:
            self._rows = [r for r in self._plan_tags if r[0] in set(params[0])]
        else:
            self._rows = [r for r in self._links if r[0] in set(params[0])]
        return self

    def fetchall(self) -> list[Any]:
        return self._rows


def test_commit_handles_maps_resolved_and_landed() -> None:
    store = _FakeStore(
        comments=[
            ("gripe", 41, "resolved: abc123def (main, release pending) — x"),
            ("todo", 77, "landed: feedbee99 — fix widgets"),
            ("todo", 78, "landed: cafef00d4 — fix with no gripe"),
            ("gripe", 42, "a comment with no marker"),
        ],
        plan_tags=[(77, "plan:900")],
        links=[(900, 55)],
    )
    sha1 = "abc123def" + "0" * 31
    sha2 = "feedbee99" + "0" * 31
    sha3 = "cafef00d4" + "0" * 31
    got = S.commit_handles(store, {sha1, sha2, sha3})
    assert got[sha1] == ["gr41"]
    # a todo's landed: resolves through its plan root's related-to gripe
    assert got[sha2] == ["gr55"]
    # … and keeps its td handle when the plan has no gripe
    assert got[sha3] == ["td78"]
    assert "gr42" not in {h for hs in got.values() for h in hs}


def test_commit_handles_prefix_match_both_directions() -> None:
    store = _FakeStore(
        comments=[("gripe", 9, "resolved: " + "ab12cd34e" + " (main)")],
    )
    long_sha = "ab12cd34e" + "f" * 31
    short_sha = "ab12cd3"
    got = S.commit_handles(store, {long_sha, short_sha})
    assert got[long_sha] == ["gr9"]
    # the short sha prefix-matches the longer comment sha (min 7 hex)
    assert got[short_sha] == ["gr9"]


# ── the failure path ───────────────────────────────────────────────


def test_failure_paths_cover_tests_imports_and_tracebacks(repo: Path) -> None:
    _commit(
        repo,
        {
            "src/precis/widgets.py": "A = 1\n",
            "tests/test_widgets.py": "from precis import widgets\n\n\ndef test_a(): ...\n",
        },
        "base",
    )
    end = _git(repo, "rev-parse", "HEAD")
    log_text = (
        '  File "/work/repo/src/precis/other.py", line 9, in boom\n'
        '  File "/venv/lib/python3.13/site-packages/precis/x.py", line 1\n'
        "  E   AssertionError\n"
    )
    got = S.failure_paths(repo, end, ["tests/test_widgets.py::test_a"], log_text)
    assert "tests/test_widgets.py" in got  # the failing test's own file
    assert "src/precis/widgets.py" in got  # the module it imports
    assert "src/precis/other.py" in got  # the traceback frame, repo-relative
    assert not any("site-packages" in p for p in got)


def test_repo_path_normalisation() -> None:
    assert S._repo_path("/w/r/src/precis/x.py") == "src/precis/x.py"
    assert S._repo_path("tests/test_a.py") == "tests/test_a.py"
    assert S._repo_path("/venv/lib/python3.13/site-packages/p/x.py") is None
    assert S._repo_path("/usr/lib/python3.13/os.py") is None


# ── rank and render ────────────────────────────────────────────────


def _stat(path: str, a: int, d: int, commits: tuple[str, ...] = ()) -> S.FileStat:
    return S.FileStat(path, a, d, commits)


def test_rank_orders_path_then_commits_then_lines() -> None:
    c1, c2 = "a" * 40, "b" * 40
    files = [
        _stat("big.py", 900, 0, (c1,)),
        _stat("many.py", 1, 0, (c1, c2)),
        _stat("onpath.py", 1, 0, (c1,)),
    ]
    rows = S.rank_rows(files, {"onpath.py"}, {c1: ["gr7"], c2: ["gr8"]})
    assert [r.path for r in rows] == ["onpath.py", "many.py", "big.py"]
    assert rows[0].on_path and rows[1].gripes == ("gr7", "gr8")


def test_render_block_marks_the_path_and_caps_rows() -> None:
    rows = [
        S.SuspectRow("onpath.py", 3, 1, ("a" * 40,), ("gr7",), True),
        S.SuspectRow("other.py", 2, 0, ("b" * 40,), (), False),
    ]
    text = S.render_block(rows, "aaa..bbb")
    assert "ranked by suspicion" in text
    assert " * onpath.py — 1 commit (+3/-1) — gr7" in text
    assert "   other.py — 1 commit (+2/-0)" in text


def test_render_block_gives_every_file_its_full_row() -> None:
    """No cap on detail: a file past row 25 still carries its commit
    count, diff stat, gripe handles and failure-path flag."""
    many = [
        S.SuspectRow(f"f{i}.py", 1, 2, ("c" * 40,), ("gr9",), i == 29)
        for i in range(30)
    ]
    text = S.render_block(many, "a..b")
    assert " * f29.py — 1 commit (+1/-2) — gr9" in text
    assert "   f28.py — 1 commit (+1/-2) — gr9" in text
    assert "view='impact'" in text
    assert "'base': 'a'" in text and "'head': 'b'" in text


def test_render_block_budget_cuts_symbols_before_files() -> None:
    sym = ChangedSymbol("precis.x.foo", "f0.py", "body")
    rows = [S.SuspectRow(f"f{i}.py", 1, 0, (), (), False, (sym,)) for i in range(10)]
    full = S.render_block(rows, "a..b")
    text = S.render_block(rows, "a..b", budget=len(full) - 200)
    assert len(text) <= len(full) - 200
    assert all(f"f{i}.py —" in text for i in range(10))
    assert "view='impact'" in text
    assert "more lines cut" in text


def test_render_block_budget_never_drops_a_file_row_or_the_pointer() -> None:
    """td655162: the complete table is the fixer's only copy — a budget
    tighter than the table itself still ships every file row and the
    fan-out pointer; only the changed-symbol tail gives room."""
    sym = ChangedSymbol("precis.x.foo", "f0.py", "body")
    rows = [
        S.SuspectRow(f"f{i}.py", 1, 0, ("c" * 40,), ("gr9",), i == 0, (sym,))
        for i in range(60)
    ]
    text = S.render_block(rows, "aaa..bbb", budget=200)
    assert all(f"f{i}.py —" in text for i in range(60))
    assert "view='impact'" in text
    assert "more lines cut" in text


def test_render_block_lists_the_changed_symbols() -> None:
    """The python kind's diff rendered as a view: each file's changed
    symbols follow the table, marked like the impact view's rows."""
    row = S.SuspectRow(
        "src/precis/x.py",
        3,
        1,
        ("a" * 40,),
        (),
        True,
        symbols=(
            ChangedSymbol("precis.x.foo", "src/precis/x.py", "body"),
            ChangedSymbol(
                "precis.x.Bar.m", "src/precis/x.py", "signature", "m(a) -> m(a, b)"
            ),
        ),
    )
    text = S.render_block([row], "aaa..bbb")
    assert "changed symbols" in text
    assert "  ~ precis.x.foo  body  src/precis/x.py" in text
    assert "signature  src/precis/x.py — m(a) -> m(a, b)" in text


# ── the changed-symbol view ────────────────────────────────────────


def test_changed_symbols_diffs_the_range(repo: Path) -> None:
    base = _commit(
        repo,
        {
            "src/precis/__init__.py": "",
            "src/precis/widgets.py": (
                "def f(a):\n    return a\n\n\ndef g():\n    return 1\n"
            ),
            "docs/x.md": "hi\n",
        },
        "base",
    )
    tip = _commit(
        repo,
        {
            "src/precis/widgets.py": (
                "def f(a, b):\n    return a + b\n\n\n"
                "def g():\n    return 2\n\n\n"
                "def h():\n    return 3\n"
            ),
        },
        "tip",
    )
    rng = f"{base}..{tip}"
    got = S.changed_symbols(repo, rng, ["src/precis/widgets.py", "docs/x.md"])
    assert got is not None
    cs = {(c.qualname, c.change) for c in got["src/precis/widgets.py"]}
    assert ("precis.widgets.f", "signature") in cs
    assert ("precis.widgets.g", "body") in cs
    assert ("precis.widgets.h", "added") in cs
    assert "docs/x.md" not in got  # the diff is a *.py-only view


def test_changed_symbols_removed_symbol_and_degrades(repo: Path) -> None:
    base = _commit(
        repo,
        {"src/precis/old.py": "def gone():\n    return 1\n", "a.txt": "x\n"},
        "base",
    )
    tip = _commit(repo, {"a.txt": "y\n", "src/precis/old.py": ""}, "tip")
    got = S.changed_symbols(repo, f"{base}..{tip}", ["src/precis/old.py", "a.txt"])
    assert got is not None
    assert ("old.gone", "removed") in {
        (c.qualname, c.change) for c in got["src/precis/old.py"]
    }
    # no *.py paths at all → the empty map, not a fault
    assert S.changed_symbols(repo, f"{base}..{tip}", ["a.txt"]) == {}
    # a non-range and an unresolvable range → None (the caller degrades)
    assert S.changed_symbols(repo, "deadbeef", ["x.py"]) is None
    assert S.changed_symbols(repo, "aaaa1111..bbbb2222", ["x.py"]) is None


# ── the assembled block ────────────────────────────────────────────


def test_suspects_block_end_to_end(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = _commit(
        repo,
        {
            "src/precis/widgets.py": "A = 1\n",
            "src/precis/other.py": "B = 1\n",
            "tests/test_widgets.py": "from precis import widgets\n\n\ndef test_a(): ...\n",
        },
        "base",
    )
    fix = _commit(repo, {"src/precis/widgets.py": "A = 2\n"}, "fix (gr41)")
    tip = _commit(repo, {"src/precis/other.py": "B = 2\n"}, "other")
    rng = f"{base}..{tip}"
    monkeypatch.setattr(S._gates, "read_ci_verdicts", lambda store: [])
    store = _FakeStore(
        comments=[("gripe", 41, f"resolved: {fix[:9]} (main, release pending)")],
    )
    log_text = (
        "== test-linux ==\n"
        "  failed ids (1):\n"
        "    tests/test_widgets.py::test_a\n"
        "  FAILED tests/test_widgets.py::test_a - AssertionError\n"
        "  =========== 1 failed, 40 passed in 3.2s ===========\n"
    )
    text = S.suspects_block(store, repo, sha=tip, rng=rng, log_text=log_text)
    assert f"changed files in {rng}" in text
    # the failing test's import ranks first, then by lines changed
    lines = text.splitlines()
    w = next(l for l in lines if "src/precis/widgets.py" in l)
    o = next(l for l in lines if "src/precis/other.py" in l)
    assert w.startswith(" *") and "gr41" in w
    assert o.startswith("  ") and "gr41" not in o
    assert lines.index(w) < lines.index(o)


def test_suspects_block_flags_the_symbol_on_the_traceback(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A traceback ``in <name>`` frame matching a changed symbol flags
    its file on the failure path even when the frame's path maps outside
    the repo — and the block lists the symbol diff under the table."""
    base = _commit(
        repo,
        {
            "src/precis/__init__.py": "",
            "src/precis/victim.py": "def explode():\n    return 1\n",
            "src/precis/bystander.py": "def calm():\n    return 1\n",
        },
        "base",
    )
    tip = _commit(
        repo,
        {
            "src/precis/victim.py": "def explode():\n    return 2\n",
            "src/precis/bystander.py": "def calm():\n    return 2\n",
        },
        "tip",
    )
    rng = f"{base}..{tip}"
    monkeypatch.setattr(S._gates, "read_ci_verdicts", lambda store: [])
    log_text = (
        "== test-linux ==\n"
        '  File "/venv/lib/python3.13/site-packages/harness.py", '
        "line 4, in explode\n"
        "  FAILED tests/test_other.py::test_a - boom\n"
        "  ============= 1 failed, 9 passed in 3.2s =============\n"
    )
    text = S.suspects_block(_FakeStore(), repo, sha=tip, rng=rng, log_text=log_text)
    rows = [l for l in text.splitlines() if ".py — " in l]
    v = next(l for l in rows if "src/precis/victim.py" in l)
    b = next(l for l in rows if "src/precis/bystander.py" in l)
    # the site-packages frame names no repo path — only the changed
    # symbol ``explode`` puts victim.py on the failure path.
    assert v.startswith(" *") and b.startswith("  ")
    assert "precis.victim.explode" in text
    assert "precis.bystander.calm" in text


def test_suspects_block_degrades_to_empty(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(S._gates, "read_ci_verdicts", lambda store: [])
    _commit(repo, {"a.txt": "x\n"}, "base")
    # not a two-sided range and no sha → ""
    assert S.suspects_block(None, repo, sha="", rng="deadbeef", log_text="") == ""
    # an unresolvable range → ""
    assert (
        S.suspects_block(None, repo, sha="", rng="aaaa1111..bbbb2222", log_text="")
        == ""
    )
