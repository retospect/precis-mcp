"""``scripts/memory-lint`` check 2 — the landed-thread scan.

Found via docs/backlog/memory-lint-threads-scan-dead.md: check 2 sliced
``MEMORY.md`` with ``awk '/^## Threads/{f=1;next} /^## /{f=0} f'``, but the
live index (``~/.claude/projects/<repo>/memory/MEMORY.md``) is a flat bullet
list with zero ``##`` headings. The slice came back empty, the scan loop
never ran, and the session-start hygiene line still printed "no landed
threads lingering" — a false negative that silenced the one auto-catch that
was supposed to drive thread retirement.

Exercised against the REAL script, never reimplemented — same technique as
tests/test_deploy_lag_honesty.py: ``scripts/memory-lint`` is copied
byte-for-byte into a throwaway ``git init`` repo at its real relative path
(it resolves its own repo root off ``dirname "$0"/..`` and, from there,
derives ``$HOME/.claude/projects/<escaped-root>/memory`` via
``git rev-parse --git-common-dir``). A throwaway repo, not this worktree, is
required: a worktree-isolated container test can't see this worktree's real
shared ``.git`` (it lives outside the mounted subtree), so any git op against
the real repo fails with "not a git repository" in-container.
"""

from __future__ import annotations

import datetime
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only bash/awk/git script"
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MEMORY_LINT_SRC = REPO_ROOT / "scripts" / "memory-lint"


def _today() -> str:
    return datetime.datetime.now(datetime.UTC).date().isoformat()


def _test_env(**extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
    )
    env.update(extra)
    return env


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), env=_test_env(), capture_output=True, text=True
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result


@pytest.fixture
def lint_repo(tmp_path: Path) -> Path:
    """A throwaway repo carrying the REAL scripts/memory-lint at its real
    relative path, plus an in-window sibling-repo weekly-gate stamp (check 6
    always runs and self-stamps; giving it a fresh stamp keeps these tests
    from tripping that unrelated append)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("root\n", encoding="utf-8")

    scripts_dir = repo / "scripts"
    scripts_dir.mkdir()
    shutil.copy2(MEMORY_LINT_SRC, scripts_dir / "memory-lint")
    (scripts_dir / "memory-lint").chmod(0o755)

    runbooks_dir = repo / "docs" / "runbooks"
    runbooks_dir.mkdir(parents=True)
    (runbooks_dir / "memory-sibling-repos.md").write_text(
        f"## Log\n\n**{_today()}** — placeholder\n", encoding="utf-8"
    )

    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add memory-lint")
    # memory-lint only reads a hex token as a sha when it contains an a-f
    # letter, so a plain digit run (a byte count, a year) isn't mistaken for
    # a commit. That makes an all-digit short sha a ~1-in-150 fixture draw
    # that empties the scan's sha list: the landed-thread tests below would
    # miss their line, and the open-work-words test would pass for the wrong
    # reason. Re-roll the commit until the short form carries a letter.
    for attempt in range(50):
        if any(c in "abcdef" for c in _landed_sha(repo)):
            break
        _git(repo, "commit", "-q", "--amend", "-m", f"add memory-lint {attempt}")
    else:  # pragma: no cover - 50 consecutive all-digit shas
        pytest.fail("no fixture sha with a hex letter after 50 attempts")
    return repo


def _main_root(repo: Path) -> str:
    """Same derivation memory-lint uses: parent of the git-common-dir."""
    out = _git(
        repo, "rev-parse", "--path-format=absolute", "--git-common-dir"
    ).stdout.strip()
    return str(Path(out).parent)


def _landed_sha(repo: Path) -> str:
    return _git(repo, "rev-parse", "--short=12", "HEAD").stdout.strip()


@pytest.fixture
def home_and_mem(lint_repo: Path, tmp_path: Path) -> tuple[Path, Path]:
    """A fake $HOME whose memory dir matches the path scripts/memory-lint
    derives from ``lint_repo``, so the script finds our fixture INDEX."""
    home = tmp_path / "home"
    escaped = _main_root(lint_repo).replace("/", "-")
    mem = home / ".claude" / "projects" / escaped / "memory"
    mem.mkdir(parents=True)
    return home, mem


def _run(
    repo: Path,
    home: Path,
    *,
    cache: Path | None = None,
    nodes: Path | None = None,
    args: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "HOME": str(home)}
    if cache is not None:
        env["PRECIS_MEMORY_CACHE"] = str(cache)
    # Never let the real ~/.cache node export leak into a test.
    env["PRECIS_MEMORY_NODES"] = str(nodes or home / "no-nodes-here")
    return subprocess.run(
        [str(repo / "scripts" / "memory-lint"), *args],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_flat_index_flags_missing_threads_heading(
    lint_repo: Path, home_and_mem: tuple[Path, Path]
) -> None:
    """A flat index (no `## ` headings at all) must surface a finding, not
    the false-clean "no landed threads lingering" line."""
    home, mem = home_and_mem
    (mem / "MEMORY.md").write_text(
        "- some durable fact about the system, no section headings here.\n"
        "- another note, still flat.\n",
        encoding="utf-8",
    )

    res = _run(lint_repo, home)

    assert res.returncode == 0
    assert "no `## Threads` heading" in res.stdout, res.stdout
    assert "no landed threads lingering" not in res.stdout, res.stdout


def test_flat_index_still_scans_every_bullet_for_landed_threads(
    lint_repo: Path, home_and_mem: tuple[Path, Path]
) -> None:
    """Even with no `## Threads` heading, a linked topic file whose every
    cited sha is an ancestor of main and carries no open-work words must
    still be flagged as landed — the scan can't depend on section structure
    that the live index doesn't have."""
    home, mem = home_and_mem
    sha = _landed_sha(lint_repo)
    (mem / "MEMORY.md").write_text(
        "- some campaign — [detail](some-topic.md)\n",
        encoding="utf-8",
    )
    (mem / "some-topic.md").write_text(
        f"state: SHIPPED. commit {sha} landed in main.\n",
        encoding="utf-8",
    )

    res = _run(lint_repo, home)

    assert res.returncode == 0
    assert "landed thread → some-topic.md" in res.stdout, res.stdout


def test_open_work_words_still_suppress_the_landed_flag(
    lint_repo: Path, home_and_mem: tuple[Path, Path]
) -> None:
    """Regression guard: the widened scan must keep honouring the
    open-work-words exclusion (a live thread with a landed sub-part isn't a
    landed thread)."""
    home, mem = home_and_mem
    sha = _landed_sha(lint_repo)
    (mem / "MEMORY.md").write_text(
        "- some campaign — [detail](some-topic.md)\n",
        encoding="utf-8",
    )
    (mem / "some-topic.md").write_text(
        f"state: SHIPPED sub-part at {sha}; NEXT step still pending.\n",
        encoding="utf-8",
    )

    res = _run(lint_repo, home)

    assert res.returncode == 0
    assert "landed thread → some-topic.md" not in res.stdout, res.stdout


def test_kebab_case_broken_link_is_reported(
    lint_repo: Path, home_and_mem: tuple[Path, Path]
) -> None:
    """gr450546: check 1a's link-extraction regex had the same missing-hyphen
    character class as check 2's — a kebab-case link (every real topic file
    name) to a MISSING target must be flagged as broken."""
    home, mem = home_and_mem
    (mem / "MEMORY.md").write_text(
        "- some campaign — [detail](some-missing-topic.md)\n",
        encoding="utf-8",
    )

    res = _run(lint_repo, home)

    assert res.returncode == 0
    assert "broken link → some-missing-topic.md" in res.stdout, res.stdout


_POINTER = (
    "<!-- memory-index: graph -->\n"
    "# Memory index\n\nThe index is rendered from the graph at session start.\n"
)


def test_graph_mode_lints_the_cached_render_not_the_topic_files(
    lint_repo: Path, home_and_mem: tuple[Path, Path], tmp_path: Path
) -> None:
    """After the cutover MEMORY.md is a pointer and the topic files are the
    import snapshot: an unindexed or landed-looking file must not be flagged,
    and the size check reads the session-start hook's cached render."""
    home, mem = home_and_mem
    sha = _landed_sha(lint_repo)
    (mem / "MEMORY.md").write_text(_POINTER, encoding="utf-8")
    (mem / "some-topic.md").write_text(
        f"state: SHIPPED. commit {sha} landed in main.\n", encoding="utf-8"
    )
    cache = tmp_path / "cache" / "memory-index.md"
    cache.parent.mkdir()
    cache.write_text(
        "# Memory index\n\n## Threads\n\n- Alpha (me1) — x\n", encoding="utf-8"
    )

    res = _run(lint_repo, home, cache=cache)

    assert res.returncode == 0
    assert "memory-lint: graph mode — index from the session-start render" in res.stdout
    assert "unindexed" not in res.stdout, res.stdout
    assert "landed thread" not in res.stdout, res.stdout
    assert "no `## Threads` heading" not in res.stdout, res.stdout
    assert "memory-lint: ✓ clean (graph mode" in res.stdout, res.stdout
    assert "preamble" not in res.stdout, res.stdout


def test_graph_mode_without_a_cached_render_says_so(
    lint_repo: Path, home_and_mem: tuple[Path, Path], tmp_path: Path
) -> None:
    home, mem = home_and_mem
    (mem / "MEMORY.md").write_text(_POINTER, encoding="utf-8")

    res = _run(lint_repo, home, cache=tmp_path / "absent.md")

    assert res.returncode == 0
    assert "graph mode — no cached render yet" in res.stdout, res.stdout
    assert "hygiene issue" not in res.stdout, res.stdout


# ---------------------------------------------------------------------------
# graph mode over the node cache (`precis memory index --export-dir`)
# ---------------------------------------------------------------------------

_NO_NODES = "no node cache yet — scripts/hooks/session-start-memory.sh writes it"


@pytest.fixture
def graph_setup(
    lint_repo: Path, home_and_mem: tuple[Path, Path], tmp_path: Path
) -> tuple[Path, Path, Path, Path]:
    """(home, memory dir with a pointer MEMORY.md, cache, node dir)."""
    home, mem = home_and_mem
    (mem / "MEMORY.md").write_text(_POINTER, encoding="utf-8")
    cache = tmp_path / "cache" / "memory-index.md"
    cache.parent.mkdir()
    cache.write_text("# Memory index\n\n- Alpha (me1) — x\n", encoding="utf-8")
    nodes = tmp_path / "nodes"
    nodes.mkdir()
    return home, mem, cache, nodes


def test_graph_mode_flags_landed_and_payload_nodes_by_handle(
    lint_repo: Path, graph_setup: tuple[Path, Path, Path, Path]
) -> None:
    home, _mem, cache, nodes = graph_setup
    sha = _landed_sha(lint_repo)
    (nodes / "me464085.md").write_text(
        f"# Old campaign\n\nstate: SHIPPED. commit {sha} landed in main.\n",
        encoding="utf-8",
    )
    (nodes / "me464086.md").write_text(
        f"# Live work\n\nSHIPPED at {sha} but NEXT slice pending.\n", encoding="utf-8"
    )
    (nodes / "me464087.md").write_text(
        "# Recipe\n\n```\nrun this\n```\n", encoding="utf-8"
    )

    res = _run(lint_repo, home, cache=cache, nodes=nodes)

    assert res.returncode == 0
    assert (
        "landed thread → me464085 (Old campaign) — verify, then "
        "delete(kind='memory', id='me464085')" in res.stdout
    ), res.stdout
    assert "me464086" not in res.stdout, res.stdout  # open-work words keep it
    assert "payload-smell → me464087 (Recipe) (1 code fence(s)" in res.stdout
    assert "edit(kind='memory', id='me464087')" in res.stdout, res.stdout
    assert "no `## Threads` heading" not in res.stdout, res.stdout
    assert "unindexed" not in res.stdout, res.stdout
    assert "memory-lint: 2 hygiene issue(s) above" in res.stdout, res.stdout


def test_graph_mode_landed_scan_reads_only_threads_nodes_from_the_manifest(
    lint_repo: Path, graph_setup: tuple[Path, Path, Path, Path]
) -> None:
    # A gotcha citing the sha of a landed fix is durable knowledge, not a
    # landed thread: with the export's _sections.tsv present, only `threads`
    # nodes are scanned.
    home, _mem, cache, nodes = graph_setup
    sha = _landed_sha(lint_repo)
    body = f"state: SHIPPED. commit {sha} landed in main.\n"
    (nodes / "me464085.md").write_text(f"# Old campaign\n\n{body}", encoding="utf-8")
    (nodes / "me464160.md").write_text(f"# Fixed trap\n\n{body}", encoding="utf-8")
    (nodes / "_sections.tsv").write_text(
        "me464085\tthreads\nme464160\tgotchas\n", encoding="utf-8"
    )

    res = _run(lint_repo, home, cache=cache, nodes=nodes)

    assert res.returncode == 0
    assert "landed thread → me464085 (Old campaign)" in res.stdout, res.stdout
    assert "me464160" not in res.stdout, res.stdout
    assert "memory-lint: 1 hygiene issue(s) above" in res.stdout, res.stdout


def test_graph_mode_currency_runs_over_the_nodes(
    lint_repo: Path, graph_setup: tuple[Path, Path, Path, Path]
) -> None:
    home, _mem, cache, nodes = graph_setup
    (nodes / "me464090.md").write_text(
        "# Stale anchor\n\nSee src/precis/not_a_real_file.py for the NEXT step.\n",
        encoding="utf-8",
    )
    (nodes / "me464091.md").write_text("# Fine\n\nNo anchors here.\n", encoding="utf-8")

    res = _run(lint_repo, home, cache=cache, nodes=nodes, args=("--currency",))

    assert res.returncode == 0
    assert "not run in graph mode" not in res.stdout, res.stdout
    assert "— currency ledger (git+fs anchors; suspects only) —" in res.stdout
    assert "  me464090 (Stale anchor):" in res.stdout, res.stdout
    assert "path src/precis/not_a_real_file.py missing on main" in res.stdout
    assert "me464091" not in res.stdout, res.stdout
    assert (
        "1 memory node(s) with stale anchors — adjust via edit(kind='memory'"
        in res.stdout
    ), res.stdout


def test_currency_ignores_worktree_named_memory_links(
    lint_repo: Path, graph_setup: tuple[Path, Path, Path, Path]
) -> None:
    # [[worktree-…]] is a memory slug, not a branch; a bare worktree name in
    # prose still counts.
    home, _mem, cache, nodes = graph_setup
    (nodes / "me464116.md").write_text(
        "# Build\n\nNEXT slice. See [[worktree-switch-breaks-subagent-bash]].\n",
        encoding="utf-8",
    )
    (nodes / "me464117.md").write_text(
        "# Other\n\nNEXT slice in worktree gone-away-tree.\n", encoding="utf-8"
    )

    res = _run(lint_repo, home, cache=cache, nodes=nodes, args=("--currency",))

    assert res.returncode == 0
    assert "switch-breaks-subagent-bash" not in res.stdout, res.stdout
    assert "branch/worktree 'gone-away-tree' gone" in res.stdout, res.stdout


def test_graph_mode_sibling_check_scans_nodes_and_stamps(
    lint_repo: Path, graph_setup: tuple[Path, Path, Path, Path]
) -> None:
    home, _mem, cache, nodes = graph_setup
    log = lint_repo / "docs" / "runbooks" / "memory-sibling-repos.md"
    log.write_text("## Log\n\n**2020-01-01** — old\n", encoding="utf-8")  # DUE
    (nodes / "me464092.md").write_text(
        "# Sibling\n\nSee ~/work/long-retired-sibling-repo for it.\n", encoding="utf-8"
    )

    res = _run(lint_repo, home, cache=cache, nodes=nodes)

    assert "sibling-repo check: DUE" in res.stdout, res.stdout
    assert (
        "me464092: ~/work/long-retired-sibling-repo — no longer on disk" in res.stdout
    ), res.stdout
    assert "1 stale path(s): me464092:" in log.read_text(encoding="utf-8")


def test_graph_mode_with_a_missing_or_empty_node_cache_skips_with_one_line(
    lint_repo: Path, graph_setup: tuple[Path, Path, Path, Path]
) -> None:
    home, _mem, cache, nodes = graph_setup
    log = lint_repo / "docs" / "runbooks" / "memory-sibling-repos.md"
    log.write_text("## Log\n\n**2020-01-01** — old\n", encoding="utf-8")  # DUE
    before = log.read_text(encoding="utf-8")

    for nodedir in (nodes, nodes / "absent"):  # empty, then missing
        res = _run(lint_repo, home, cache=cache, nodes=nodedir, args=("--currency",))
        assert res.returncode == 0
        assert res.stdout.count(_NO_NODES) == 1, res.stdout
        assert "landed thread" not in res.stdout, res.stdout
        assert "payload-smell" not in res.stdout, res.stdout
        assert "currency ledger" not in res.stdout, res.stdout
        assert "sibling-repo check" not in res.stdout, res.stdout
        assert "hygiene issue" not in res.stdout, res.stdout
        assert log.read_text(encoding="utf-8") == before  # nothing scanned, no stamp


def test_graph_mode_stray_write_check_compares_to_pre_cutover(
    lint_repo: Path, graph_setup: tuple[Path, Path, Path, Path]
) -> None:
    home, mem, cache, nodes = graph_setup
    pre = mem / "MEMORY.md.pre-cutover"
    pre.write_text("- old index\n", encoding="utf-8")
    old = mem / "frozen-snapshot.md"
    old.write_text("snapshot\n", encoding="utf-8")
    stray = mem / "fleet-say-drops-first-line.md"
    stray.write_text("edited after the cutover\n", encoding="utf-8")
    log = mem / "memory_consolidation_log.md"
    log.write_text("2026-10-03 pass\n", encoding="utf-8")
    os.utime(pre, (1_700_000_000, 1_700_000_000))
    os.utime(old, (1_699_999_000, 1_699_999_000))  # older than the reference
    for f in (stray, log, mem / "MEMORY.md"):  # newer; the last two are exempt
        os.utime(f, (1_700_001_000, 1_700_001_000))

    res = _run(lint_repo, home, cache=cache, nodes=nodes)

    assert res.returncode == 0
    assert (
        "stray write → fleet-say-drops-first-line.md (edited after the cutover; "
        "the graph never reads it — port the body into its node by handle, then "
        "clear this with: touch -r " in res.stdout
    ), res.stdout
    assert "frozen-snapshot.md" not in res.stdout, res.stdout
    assert "stray write → memory_consolidation_log.md" not in res.stdout
    assert "stray write → MEMORY.md" not in res.stdout, res.stdout
    assert "memory-lint: 1 hygiene issue(s) above" in res.stdout, res.stdout

    pre.unlink()  # no reference point -> the check has nothing to compare
    res = _run(lint_repo, home, cache=cache, nodes=nodes)
    assert "stray write" not in res.stdout, res.stdout


def test_graph_mode_reconsolidation_due_names_the_graph_verbs(
    lint_repo: Path, graph_setup: tuple[Path, Path, Path, Path]
) -> None:
    home, mem, cache, nodes = graph_setup
    (mem / "memory_consolidation_log.md").write_text(
        "2020-01-01 an old pass\n", encoding="utf-8"
    )

    res = _run(lint_repo, home, cache=cache, nodes=nodes)

    line = next(ln for ln in res.stdout.splitlines() if ln.startswith("reconsol"))
    assert "DUE (last 2020-01-01)" in line
    assert "edit(kind='memory'" in line and "delete(kind='memory'" in line
    assert "topic files" not in line, line

    # file mode keeps the original wording
    (mem / "MEMORY.md").write_text("- flat\n", encoding="utf-8")
    res = _run(lint_repo, home)
    line = next(ln for ln in res.stdout.splitlines() if ln.startswith("reconsol"))
    assert "edit(kind='memory'" not in line, line
    assert "adjust / kill / promote-to-doc" in line


def test_file_mode_ignores_the_node_cache_and_the_stray_check(
    lint_repo: Path, home_and_mem: tuple[Path, Path], tmp_path: Path
) -> None:
    """Without the graph marker nothing reads the node dir, and a file newer
    than a leftover MEMORY.md.pre-cutover is not a finding."""
    home, mem = home_and_mem
    sha = _landed_sha(lint_repo)
    (mem / "MEMORY.md").write_text(
        "## Threads\n\n- some campaign — [detail](some-topic.md)\n", encoding="utf-8"
    )
    (mem / "some-topic.md").write_text(
        f"state: SHIPPED. commit {sha} landed in main.\n", encoding="utf-8"
    )
    pre = mem / "MEMORY.md.pre-cutover"
    pre.write_text("x\n", encoding="utf-8")
    os.utime(pre, (1_700_000_000, 1_700_000_000))
    nodes = tmp_path / "nodes"
    nodes.mkdir()
    (nodes / "me1.md").write_text("# N\n\n```\nx\n```\n", encoding="utf-8")

    res = _run(lint_repo, home, nodes=nodes)

    assert res.returncode == 0
    assert "landed thread → some-topic.md (all cited commits in main" in res.stdout
    assert "stray write" not in res.stdout, res.stdout
    assert "me1" not in res.stdout, res.stdout
    assert "graph mode" not in res.stdout, res.stdout


# ---------------------------------------------------------------------------
# check 7 — retire candidates (listed only, both modes)
# ---------------------------------------------------------------------------


def _snapshot(d: Path) -> dict[str, tuple[bytes, int]]:
    return {
        p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in sorted(d.glob("*.md"))
    }


def _age(p: Path, days: int) -> None:
    t = datetime.datetime.now(datetime.UTC).timestamp() - days * 86400
    os.utime(p, (t, t))


def test_retire_candidates_file_mode_lists_landed_and_stale_only(
    lint_repo: Path, home_and_mem: tuple[Path, Path]
) -> None:
    home, mem = home_and_mem
    sha = _landed_sha(lint_repo)
    # a REAL commit that is not an ancestor of main
    _git(lint_repo, "checkout", "-q", "-b", "side")
    (lint_repo / "x.txt").write_text("x\n", encoding="utf-8")
    _git(lint_repo, "add", "-A")
    _git(lint_repo, "commit", "-q", "-m", "side commit")
    side = _git(lint_repo, "rev-parse", "--short=12", "HEAD").stdout.strip()
    _git(lint_repo, "checkout", "-q", "main")
    (mem / "MEMORY.md").write_text(
        "## Threads\n- [a](done-thread.md)\n- [b](live-thread.md)\n", encoding="utf-8"
    )
    (mem / "done-thread.md").write_text(
        f"---\ntype: project\n---\nlanded in {sha}.\nNEXT: done\n", encoding="utf-8"
    )
    (mem / "live-thread.md").write_text(
        f"---\ntype: project\n---\nlanded {sha}; unlanded {side}.\nNEXT: done\n",
        encoding="utf-8",
    )
    (mem / "old-fact.md").write_text("durable fact\n", encoding="utf-8")
    _age(mem / "old-fact.md", 40)
    before = _snapshot(mem)

    res = _run(lint_repo, home)

    assert res.returncode == 0
    assert "retire candidate → done-thread.md: thread landed" in res.stdout, res.stdout
    assert "retire candidate → live-thread.md" not in res.stdout, res.stdout
    assert "retire candidate → old-fact.md: stale >30d (40d)" in res.stdout, res.stdout
    assert "retire candidates: 2 " in res.stdout, res.stdout
    assert _snapshot(mem) == before  # lists only; nothing modified


def test_retire_candidates_graph_mode_reads_the_node_cache(
    lint_repo: Path, graph_setup: tuple[Path, Path, Path, Path]
) -> None:
    home, _mem, cache, nodes = graph_setup
    sha = _landed_sha(lint_repo)
    (nodes / "me1.md").write_text(
        f"# Old campaign\n\nshipped at {sha}.\nNEXT = nothing\n", encoding="utf-8"
    )
    (nodes / "me2.md").write_text("# Gotcha\n\nold trap\n", encoding="utf-8")
    _age(nodes / "me2.md", 40)
    (nodes / "me3.md").write_text("# Fresh gotcha\n\nnew trap\n", encoding="utf-8")
    before = _snapshot(nodes)

    res = _run(lint_repo, home, cache=cache, nodes=nodes)

    assert res.returncode == 0
    assert "retire candidate → me1 (Old campaign): thread landed" in res.stdout
    assert "retire candidate → me2 (Gotcha): stale >30d (40d)" in res.stdout
    assert "me3" not in res.stdout, res.stdout
    assert "retire candidates: 2 " in res.stdout, res.stdout
    assert _snapshot(nodes) == before


def _days_ago(days: int) -> str:
    then = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=days)
    return then.strftime("%Y-%m-%d")


def test_graph_mode_stale_reads_the_manifest_date_not_the_export_mtime(
    lint_repo: Path, graph_setup: tuple[Path, Path, Path, Path]
) -> None:
    # The export rewrites every file each session, so file mtimes are all
    # "now"; the manifest's updated column is the node's real age.
    home, _mem, cache, nodes = graph_setup
    (nodes / "me2.md").write_text("# Gotcha\n\nold trap\n", encoding="utf-8")
    (nodes / "me3.md").write_text("# Fresh gotcha\n\nnew trap\n", encoding="utf-8")
    (nodes / "_sections.tsv").write_text(
        f"me2\tgotchas\t{_days_ago(40)}\t8\t8\t1\t0\t0\n"
        f"me3\tgotchas\t{_days_ago(1)}\t8\t8\t1\t0\t0\n",
        encoding="utf-8",
    )

    res = _run(lint_repo, home, cache=cache, nodes=nodes)

    assert "retire candidate → me2 (Gotcha): stale >30d (40d)" in res.stdout, res.stdout
    assert "me3" not in res.stdout, res.stdout


def test_graph_mode_flags_a_thread_node_untouched_14_days(
    lint_repo: Path, graph_setup: tuple[Path, Path, Path, Path]
) -> None:
    home, _mem, cache, nodes = graph_setup
    (nodes / "me2.md").write_text("# Old thread\n\nLeft: x\n", encoding="utf-8")
    (nodes / "me3.md").write_text("# Live thread\n\nLeft: y\n", encoding="utf-8")
    (nodes / "me4.md").write_text("# Old gotcha\n\ntrap\n", encoding="utf-8")
    (nodes / "_sections.tsv").write_text(
        f"me2\tthreads\t{_days_ago(15)}\t8\t8\t1\t0\t0\n"
        f"me3\tthreads\t{_days_ago(13)}\t8\t8\t1\t0\t0\n"
        f"me4\tgotchas\t{_days_ago(15)}\t8\t8\t1\t0\t0\n",
        encoding="utf-8",
    )

    res = _run(lint_repo, home, cache=cache, nodes=nodes)

    assert (
        "stale thread → me2 (Old thread) (15d untouched): retire, or move what "
        "matters to its docs/backlog/threads file / a gotcha / a todo" in res.stdout
    ), res.stdout
    assert "stale thread → me3" not in res.stdout, res.stdout
    assert "stale thread → me4" not in res.stdout, res.stdout  # not a thread


def test_graph_mode_unsectioned_manifest_is_a_finding_and_scans_every_node(
    lint_repo: Path, graph_setup: tuple[Path, Path, Path, Path]
) -> None:
    # 2026-10-09: no node carried a section tag, so the threads-only scan
    # covered nothing and reported clean. A section node (`index`) alone does
    # not count as sectioning.
    home, _mem, cache, nodes = graph_setup
    sha = _landed_sha(lint_repo)
    (nodes / "me5.md").write_text(
        f"# Old campaign\n\nstate: SHIPPED. commit {sha} landed in main.\n",
        encoding="utf-8",
    )
    today = _days_ago(0)
    (nodes / "_sections.tsv").write_text(
        f"me4\tindex\t{today}\t8\t8\t1\t0\t0\nme5\t\t{today}\t8\t8\t1\t0\t0\n",
        encoding="utf-8",
    )

    res = _run(lint_repo, home, cache=cache, nodes=nodes)

    assert "no memory node carries a section:<slug> tag" in res.stdout, res.stdout
    assert "landed thread → me5 (Old campaign)" in res.stdout, res.stdout
    assert "memory-lint: 2 hygiene issue(s) above" in res.stdout, res.stdout


def test_graph_mode_flags_the_fisheye_shape_from_the_manifest(
    lint_repo: Path, graph_setup: tuple[Path, Path, Path, Path]
) -> None:
    home, _mem, cache, nodes = graph_setup
    for h in ("me6", "me7", "me8", "me9"):
        (nodes / f"{h}.md").write_text(f"# Note {h}\n\nbody\n", encoding="utf-8")
    today = _days_ago(0)
    (nodes / "_sections.tsv").write_text(
        f"me1\tindex\t{today}\t17000\t4000\t150\t139\t1\n"  # section node, no file
        f"me6\tgotchas\t{today}\t9000\t4000\t2\t0\t0\n"
        f"me7\tgotchas\t{today}\t100\t100\t0\t0\t0\n"
        f"me8\tgotchas\t{today}\t100\t100\t3\t0\t2\n"
        f"me9\tgotchas\t{today}\t100\t100\t3\t0\t0\n",
        encoding="utf-8",
    )

    res = _run(lint_repo, home, cache=cache, nodes=nodes)

    out = res.stdout
    assert "eye-truncated → me6 (Note me6): fisheye shows 4000 of 9000 chars" in out, (
        out
    )
    assert "eye-truncated → me1 (section node): fisheye shows 4000 of 17000" in out, out
    # a section:index hub lists its members uncapped: never "flat"
    assert "flat hub → me1" not in out, out
    assert "dead links → me1 (section node): 1 link(s)" in out, out
    assert "orphan → me7 (Note me7): no live links" in out, out
    assert "dead links → me8 (Note me8): 2 link(s)" in out, out
    assert "me9" not in out, out
    assert "memory-lint: 5 hygiene issue(s) above" in out, out


def test_graph_mode_hub_checks_from_the_hub_columns(
    lint_repo: Path, graph_setup: tuple[Path, Path, Path, Path]
) -> None:
    home, _mem, cache, nodes = graph_setup
    for h in ("me11", "me12", "me13", "me14"):
        (nodes / f"{h}.md").write_text(f"# Note {h}\n\nbody\n", encoding="utf-8")
    today = _days_ago(0)
    rows = [f"me1\tindex\t{today}\t10\t10\t45\t0\t0\t-\t0"]  # 45 members
    # me11: hubless; me12: thread, hub has gotchas, none linked; me13: thread
    # with a qualified-by edge; me14: the hub's gotcha
    rows += [
        f"me11\tgotchas\t{today}\t10\t10\t1\t0\t0\t\t0",
        f"me12\tthreads\t{today}\t10\t10\t1\t0\t0\tme1\t0",
        f"me13\tthreads\t{today}\t10\t10\t2\t0\t0\tme1\t1",
        f"me14\tgotchas\t{today}\t10\t10\t2\t0\t0\tme1\t0",
    ]
    rows += [f"me{100 + i}\truns\t{today}\t10\t10\t1\t0\t0\tme1\t0" for i in range(42)]
    (nodes / "_sections.tsv").write_text("\n".join(rows) + "\n", encoding="utf-8")

    out = _run(lint_repo, home, cache=cache, nodes=nodes).stdout

    assert "big hub → me1 (section node): 45 members — split into sub-hubs" in out, out
    assert "no hub → me11 (Note me11): not part-of any section:index hub" in out, out
    assert "link(kind='memory', id='me11', target=<hub>, rel='part-of')" in out, out
    assert "unqualified thread → me12 (Note me12): hub me1 has gotchas" in out, out
    assert "unqualified thread → me13" not in out, out
    assert "no hub → me14" not in out and "no hub → me12" not in out, out


def test_graph_mode_skips_hub_checks_on_an_eight_column_manifest(
    lint_repo: Path, graph_setup: tuple[Path, Path, Path, Path]
) -> None:
    home, _mem, cache, nodes = graph_setup
    (nodes / "me6.md").write_text("# Note\n\nbody\n", encoding="utf-8")
    today = _days_ago(0)
    (nodes / "_sections.tsv").write_text(
        f"me6\tthreads\t{today}\t10\t10\t1\t0\t0\n", encoding="utf-8"
    )
    out = _run(lint_repo, home, cache=cache, nodes=nodes).stdout
    assert "no hub" not in out and "unqualified" not in out, out
    assert "memory-lint: ✓ clean" in out, out


def test_graph_mode_skips_the_shape_check_on_a_two_column_manifest(
    lint_repo: Path, graph_setup: tuple[Path, Path, Path, Path]
) -> None:
    # an export from before the shape columns: sections only, no shape lint
    home, _mem, cache, nodes = graph_setup
    (nodes / "me6.md").write_text("# Note\n\nbody\n", encoding="utf-8")
    (nodes / "_sections.tsv").write_text("me6\tgotchas\n", encoding="utf-8")

    res = _run(lint_repo, home, cache=cache, nodes=nodes)

    assert "memory-lint: ✓ clean" in res.stdout, res.stdout


def test_graph_mode_flags_cold_nodes_from_the_access_column(
    lint_repo: Path, graph_setup: tuple[Path, Path, Path, Path]
) -> None:
    home, _mem, cache, nodes = graph_setup
    for h in ("me21", "me22", "me23", "me24"):
        (nodes / f"{h}.md").write_text(f"# Note {h}\n\nbody\n", encoding="utf-8")
    old, today = _days_ago(90), _days_ago(0)
    (nodes / "_sections.tsv").write_text(
        f"me1\tindex\t{old}\t10\t10\t4\t0\t0\t-\t0\t\n"  # hubs are never cold
        f"me21\treference\t{old}\t10\t10\t1\t0\t0\tme1\t0\t\n"  # cold
        f"me22\treference\t{old}\t10\t10\t1\t0\t0\tme1\t0\t{today}\n"  # recalled
        f"me23\treference\t{today}\t10\t10\t1\t0\t0\tme1\t0\t\n"  # updated
        f"me24\treference\t{old}\t10\t10\t1\t0\t0\tme1\t0\t{old}\n",  # cold
        encoding="utf-8",
    )

    out = _run(lint_repo, home, cache=cache, nodes=nodes).stdout

    assert f"cold → me21 (Note me21): not recalled or updated since {old}" in out, out
    assert f"cold → me24 (Note me24): not recalled or updated since {old}" in out, out
    assert "cold → me22" not in out and "cold → me23" not in out, out
    assert "cold → me1 " not in out, out


def test_graph_mode_reports_open_review_todos_from_the_review_column(
    lint_repo: Path, graph_setup: tuple[Path, Path, Path, Path]
) -> None:
    home, _mem, cache, nodes = graph_setup
    for h in ("me31", "me32", "me33"):
        (nodes / f"{h}.md").write_text(f"# Note {h}\n\nbody\n", encoding="utf-8")
    today, old = _days_ago(0), _days_ago(10)
    (nodes / "_sections.tsv").write_text(
        f"me1\tindex\t{today}\t10\t10\t4\t0\t0\t-\t0\t\t\t\n"
        f"me31\treference\t{today}\t10\t10\t1\t0\t0\tme1\t0\t\t\ttd7:{today}\n"
        f"me32\treference\t{today}\t10\t10\t1\t0\t0\tme1\t0\t\t\ttd8:{old}\n"
        f"me33\treference\t{today}\t10\t10\t1\t0\t0\tme1\t0\t\t\t\n",
        encoding="utf-8",
    )

    res = _run(lint_repo, home, cache=cache, nodes=nodes)
    out = res.stdout

    assert f"under review → me31 (Note me31): td7 (opened {today})" in out, out
    assert f"stalled review → me32 (Note me32): td8 open since {old}" in out, out
    assert "review → me33" not in out, out
    assert "memory-lint: 1 hygiene issue(s) above" in out, out  # only the stalled one


def test_unqualified_thread_inherits_parent_and_honours_gotchas_none(
    lint_repo: Path, graph_setup: tuple[Path, Path, Path, Path]
) -> None:
    home, _mem, cache, nodes = graph_setup
    for h in ("me21", "me22", "me23", "me24", "me25"):
        (nodes / f"{h}.md").write_text(f"# Note {h}\n\nbody\n", encoding="utf-8")
    today = _days_ago(0)
    # cols: handle sec upd chars eye live hidden dead hub qual accessed parent review gnone
    rows = [
        f"me1\tindex\t{today}\t10\t10\t9\t0\t0\t-\t0",
        f"me20\tgotchas\t{today}\t10\t10\t2\t0\t0\tme1\t0",
        f"me21\tthreads\t{today}\t10\t10\t2\t0\t0\tme1\t1\t\tme1\t",
        f"me22\tthreads\t{today}\t10\t10\t2\t0\t0\tme1\t0\t\tme21\t",  # child of qualified
        f"me23\tthreads\t{today}\t10\t10\t2\t0\t0\tme1\t0\t\tme1\t\t1",  # gotchas:none
        f"me24\tthreads\t{today}\t10\t10\t2\t0\t0\tme1\t0\t\tme1\t",  # flagged
        f"me25\tthreads\t{today}\t10\t10\t2\t0\t0\tme1\t0\t\tme24\t",  # child of flagged
    ]
    (nodes / "_sections.tsv").write_text("\n".join(rows) + "\n", encoding="utf-8")

    out = _run(lint_repo, home, cache=cache, nodes=nodes).stdout

    assert "unqualified thread → me24" in out, out
    for h in ("me21", "me22", "me23", "me25"):
        assert f"unqualified thread → {h}" not in out, out
    assert "tag gotchas:none" in out, out


def test_bare_next_heading_with_list_keeps_thread_open(
    lint_repo: Path, graph_setup: tuple[Path, Path, Path, Path]
) -> None:
    home, _mem, cache, nodes = graph_setup
    sha = _landed_sha(lint_repo)
    (nodes / "me31.md").write_text(
        f"# Old campaign\n\nshipped at {sha}.\n\nNEXT:\n\n1. do the thing\n2. more\n",
        encoding="utf-8",
    )
    (nodes / "me32.md").write_text(
        f"# Done campaign\n\nshipped at {sha}.\n\nNEXT:\n", encoding="utf-8"
    )
    out = _run(lint_repo, home, cache=cache, nodes=nodes).stdout
    assert "retire candidate → me31 (Old campaign): thread landed" not in out, out
    assert "retire candidate → me32 (Done campaign): thread landed" in out, out
