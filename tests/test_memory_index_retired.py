"""Retirement test 4a — the graph serves ``MEMORY.md``'s index role.

docs/backlog/memory-native-authoring.md test 4a / AC 3: seed the synthetic
``tests/fixtures/file_mirror/`` tree through the importer, render
``precis memory index`` against the test DB, and the output must carry the
fixture's ``## Section`` headers and, for each fixture ``- [Title](slug.md)
— hook`` bullet, ``- Title (me<id>) — hook`` (the graph node is the truth
now, addressed by handle), in order. A green test is the condition for
deleting the ``MEMORY.md`` index by hand.

gr499042 update: ``scripts/hooks/session-start-memory.sh`` no longer prints
the block — hook output is injected once and carried on every later turn,
so it prints the ``--for-hook`` one-line counts summary plus the gripe
digest instead. The block contract below is asserted on the CLI render
(the same interpreter via the ``PRECIS_MEMORY_CLI`` seam); the hook tests
pin the summary shape, the node export, the last-good cache and the
fail-soft paths.

``PRECIS_MEMORY_CLI`` is the seam: it points the hook at this interpreter's
CLI entry point instead of ``uv run precis`` (no uv inside the gate
container). ``PRECIS_MEMORY_CACHE`` keeps the hook's last-good copy under
``tmp_path``, and ``PRECIS_HARNESS_MEMORY_MD`` points the hook's cutover
gate at a fixture ``MEMORY.md`` (absent by default, which counts as cut
over).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from precis.cli.memory import import_memory_dir
from precis.store import Store
from tests.conftest import _active_dsn

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the session-start hook is a POSIX bash script"
)

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / "scripts" / "hooks" / "session-start-memory.sh"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "file_mirror"

SUMMARY_RE = re.compile(
    r"🧠 memory: (?:\d+ live threads?|no live threads).*"
    r"'precis memory index' for the list"
)
GRIPES_RE = re.compile(
    r"gripes: \d+ open · \d+ older than 3d.* — get\(kind='gripe', id='/open'\)"
)


def _cache(tmp_path: Path) -> Path:
    return tmp_path / "cache" / "memory-index.md"


def _nodes(tmp_path: Path) -> Path:
    return tmp_path / "cache" / "memory-nodes"


def _hook_env(
    tmp_path: Path, dsn: str, nodes: Path | None, memory_md: Path | None
) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "PRECIS_DATABASE_URL": dsn,
            "PRECIS_MEMORY_CLI": f"{sys.executable} -m precis.cli.main",
            "PRECIS_MEMORY_CACHE": str(_cache(tmp_path)),
            "PRECIS_MEMORY_NODES": str(nodes or _nodes(tmp_path)),
            "PRECIS_HARNESS_MEMORY_MD": str(
                memory_md or tmp_path / "absent" / "MEMORY.md"
            ),
        }
    )
    return env


def _run_hook(
    tmp_path: Path,
    dsn: str,
    *,
    memory_md: Path | None = None,
    nodes: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(HOOK)],
        cwd=str(REPO_ROOT),
        env=_hook_env(tmp_path, dsn, nodes, memory_md),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )


def _run_cli(dsn: str, *args: str) -> subprocess.CompletedProcess[str]:
    """``precis memory index <args>`` through the same interpreter seam —
    the render the hook's summary line now points at."""
    return subprocess.run(
        [sys.executable, "-m", "precis.cli.main", "memory", "index", *args],
        cwd=str(REPO_ROOT),
        env={**os.environ, "PRECIS_DATABASE_URL": dsn},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )


def _fixture_index_lines() -> list[str]:
    text = (FIXTURE / "MEMORY.md").read_text(encoding="utf-8")
    return [ln for ln in text.splitlines() if ln.startswith(("## ", "- ["))]


def _fixture_bullets() -> list[str]:
    return [ln for ln in _fixture_index_lines() if ln.startswith("- [")]


def _assert_matches_fixture(got: list[str]) -> None:
    """``got`` = the header + bullet lines of the live-thread render: every
    fixture ``## Threads`` bullet shows as ``- me<digits> Title — hook``
    (newly imported = within the 14-day window; the body's first line is not
    ``Left:``, so the lead falls back to the hook)."""
    assert got[0] == "## Live threads (edited ≤14 days)"
    want = {
        m["title"]: m["hook"]
        for ln in _fixture_bullets()[:3]
        if (m := re.fullmatch(r"- \[(?P<title>[^\]]+)\]\([^)]+\) — (?P<hook>.*)", ln))
    }
    assert len(got) == 1 + len(want)
    seen = set()
    for g in got[1:]:
        m = re.fullmatch(r"- me\d+ (?P<title>.+?) — (?P<hook>.*)", g)
        assert m is not None, g
        lead = want[m["title"]]
        if len(lead) > 160:
            lead = lead[:159].rstrip() + "…"
        assert m["hook"] == lead, g
        seen.add(m["title"])
    assert seen == set(want)


def test_index_render_is_the_live_threads_block(store: Store) -> None:
    import_memory_dir(store, FIXTURE)
    proc = _run_cli(_active_dsn())
    assert proc.returncode == 0, proc.stderr
    got = [ln for ln in proc.stdout.splitlines() if ln.startswith(("## ", "- "))]
    _assert_matches_fixture(got)
    # the render adds nothing but the live-thread block (nothing older here)
    assert proc.stdout.startswith("## Live threads")
    assert "older thread nodes" not in proc.stdout


def test_index_render_over_budget_drops_threads_into_the_older_count(
    store: Store,
) -> None:
    import_memory_dir(store, FIXTURE)
    proc = _run_cli(_active_dsn(), "--budget-tok", "0")
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.rstrip("\n").splitlines()
    assert lines[0] == "## Live threads (edited ≤14 days)"
    assert lines[1].startswith("(+3 older thread nodes: search(kind='memory'")
    assert len(lines) == 2


def test_hook_output_is_one_summary_line_plus_gripes(
    store: Store, tmp_path: Path
) -> None:
    """gr499042: what SessionStart carries is the counts line + the gripe
    digest — bounded bytes, no thread roster."""
    import_memory_dir(store, FIXTURE)
    proc = _run_hook(tmp_path, _active_dsn())
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.rstrip("\n").splitlines()
    assert SUMMARY_RE.fullmatch(lines[0]), lines[0]
    assert lines[0].startswith("🧠 memory: 3 live threads"), lines[0]
    assert GRIPES_RE.fullmatch(lines[1]), lines[1]
    assert len(lines) == 2
    assert "## " not in proc.stdout and not any(ln.startswith("- me") for ln in lines)
    # The hook's whole contribution stays far under the ~600 B SessionStart
    # budget even with the digest appended.
    assert len(proc.stdout.encode("utf-8")) <= 400


def test_hook_exports_the_node_bodies_next_to_the_cache(
    store: Store, tmp_path: Path
) -> None:
    import_memory_dir(store, FIXTURE)
    proc = _run_hook(tmp_path, _active_dsn())
    assert proc.returncode == 0, proc.stderr
    files = sorted(_nodes(tmp_path).glob("me*.md"))
    assert len(files) == len(_fixture_bullets())  # one per topic node, no sections
    assert all(f.read_text(encoding="utf-8").startswith("# ") for f in files)
    # every thread handle in the CLI render has its exported body
    rendered = _run_cli(_active_dsn())
    printed = re.findall(r"^- (me\d+) ", rendered.stdout, re.M)
    assert len(printed) == 3 and {f.stem for f in files} >= set(printed)

    # a dead DB leaves the last good node set (and the index copy) untouched
    before = {f.name: f.read_text(encoding="utf-8") for f in files}
    dead = _run_hook(tmp_path, "postgresql://nobody@localhost:1/none?connect_timeout=3")
    assert dead.returncode == 0, dead.stderr
    assert {
        f.name: f.read_text(encoding="utf-8") for f in _nodes(tmp_path).glob("*.md")
    } == before


def test_hook_rotates_node_sets_keeps_newest_seven_and_skips_failures(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import_memory_dir(store, FIXTURE)
    monkeypatch.setenv("PRECIS_MEMORY_ROTATE_SECS", "0")
    cache = _nodes(tmp_path).parent
    rotated = lambda: sorted(cache.glob("memory-nodes.2*"))
    first = _run_hook(tmp_path, _active_dsn())
    assert first.returncode == 0, first.stderr
    assert rotated() == []  # nothing existed to rotate on the first export
    for i in range(9):
        proc = _run_hook(tmp_path, _active_dsn())
        assert proc.returncode == 0, proc.stderr
        if i == 0:
            assert len(rotated()) == 1
        time.sleep(1.05)  # stamps are per second
    kept = rotated()
    assert len(kept) == 7
    assert (kept[0] / "_sections.tsv").is_file()
    # a failed export never rotates or prunes
    dead = _run_hook(tmp_path, "postgresql://nobody@localhost:1/none?connect_timeout=3")
    assert dead.returncode == 0, dead.stderr
    assert rotated() == kept
    assert not list(cache.glob("memory-nodes.snap.*"))
    # the default interval suppresses a second rotation inside the window
    monkeypatch.delenv("PRECIS_MEMORY_ROTATE_SECS")
    again = _run_hook(tmp_path, _active_dsn())
    assert again.returncode == 0, again.stderr
    assert rotated() == kept


def test_hook_unwritable_node_dir_does_not_change_the_index_or_exit_code(
    store: Store, tmp_path: Path
) -> None:
    import_memory_dir(store, FIXTURE)
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, so the node dir cannot be created", encoding="utf-8")
    proc = _run_hook(tmp_path, _active_dsn(), nodes=blocker / "memory-nodes")
    assert proc.returncode == 0, proc.stderr
    assert SUMMARY_RE.fullmatch(proc.stdout.splitlines()[0])
    assert "node export" in proc.stderr
    assert _cache(tmp_path).read_text(encoding="utf-8") == proc.stdout


def test_hook_dead_dsn_prints_one_line_and_exits_zero(tmp_path: Path) -> None:
    proc = _run_hook(tmp_path, "postgresql://nobody@localhost:1/none?connect_timeout=3")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout.rstrip("\n").splitlines()
    assert len(out) == 1
    assert out[0].startswith("memory index unavailable:")
    assert "postgresql://" not in out[0]


def test_hook_dead_dsn_prints_the_last_good_copy(store: Store, tmp_path: Path) -> None:
    import_memory_dir(store, FIXTURE)
    good = _run_hook(tmp_path, _active_dsn())
    assert good.returncode == 0, good.stderr
    assert _cache(tmp_path).read_text(encoding="utf-8") == good.stdout
    assert not list(_cache(tmp_path).parent.glob("*.tmp.*"))

    proc = _run_hook(tmp_path, "postgresql://nobody@localhost:1/none?connect_timeout=3")
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.rstrip("\n").splitlines()
    assert proc.stdout.startswith(good.stdout)
    assert lines[-1].startswith("(memory index: precis memory index exited")
    assert "showing the cached copy from " in lines[-1]
    assert "postgresql://" not in proc.stdout


def test_hook_unrunnable_cli_prints_one_line_and_exits_zero(tmp_path: Path) -> None:
    env_proc = subprocess.run(
        ["bash", str(HOOK)],
        cwd=str(REPO_ROOT),
        env={
            **os.environ,
            "PRECIS_DATABASE_URL": "postgresql://nobody@localhost:1/none",
            "PRECIS_MEMORY_CLI": "/nonexistent/precis-cli",
            "PRECIS_MEMORY_CACHE": str(_cache(tmp_path)),
            "PRECIS_HARNESS_MEMORY_MD": str(tmp_path / "absent" / "MEMORY.md"),
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert env_proc.returncode == 0
    out = env_proc.stdout.rstrip("\n").splitlines()
    assert len(out) == 1 and out[0].startswith("memory index unavailable:")


def test_hook_is_silent_while_memory_md_is_still_the_index(tmp_path: Path) -> None:
    """Before the cutover MEMORY.md (no graph marker) is the index; printing
    the graph render too would double it in every session."""
    memory_md = tmp_path / "MEMORY.md"
    memory_md.write_text(
        "# Memory index\n\n## Threads\n\n- [A](a.md) — x\n", encoding="utf-8"
    )
    proc = _run_hook(
        tmp_path, "postgresql://nobody@localhost:1/none", memory_md=memory_md
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
    assert not _cache(tmp_path).exists()


def test_hook_prints_once_memory_md_carries_the_graph_marker(
    store: Store, tmp_path: Path
) -> None:
    import_memory_dir(store, FIXTURE)
    memory_md = tmp_path / "MEMORY.md"
    memory_md.write_text(
        "<!-- memory-index: graph -->\n# Memory index\n", encoding="utf-8"
    )
    proc = _run_hook(tmp_path, _active_dsn(), memory_md=memory_md)
    assert proc.returncode == 0, proc.stderr
    assert SUMMARY_RE.fullmatch(proc.stdout.splitlines()[0])
