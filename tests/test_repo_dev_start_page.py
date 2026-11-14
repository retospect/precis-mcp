"""gr564575 — the repo-dev start page is a fixed-id, compact index, and
``scripts/docs-index`` renders a generated, revisioned fallback of it.

``src/precis/data/skills/precis-repo-dev-start-help.md`` is the start
page a coding session loads once. The slug is load-bearing — a rename
silently breaks the load line, the fallback and any
``PRECIS_STARTUP_SKILLS`` pin — so it is pinned here.

Delivery has two halves:

- *Served*: the skill resolves through the same ``startup_skills``
  loader the cold-start banner uses, and stays an *index* — pointers to
  the memory layer, repo docs and the canonical skill owners, never an
  inlined manual (the byte cap ratchets that shape).
- *Local*: ``docs-index`` writes ``docs/repo-dev-start-page.md`` per
  worktree — the start page and the ``soul`` conduct-layer index
  embedded verbatim from their canonical files (each rev-stamped, no
  maintained copy), plus the missing-MCP pointers. That is the cold
  parent/child delivery the SessionStart wiring needs: every tree gets
  its own render from its own checkout, no database or network, so the
  file exists even mid-outage — the "generated, revisioned fallback"
  and "full compact index at startup" of gr564575's design.

The shared ``outage-memory.md`` address is pinned against the
SessionStart hook's own derivation so the fallback, the hook and
``scripts/memory-lint`` can never disagree on where the shared
bootstrap lives.

Pure file stats, ``startup_skills.resolve`` and ``docs-index`` renders —
no DB.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from precis import startup_skills
from precis.utils.skill_common import parse_frontmatter
from tests._script import load_script
from tests._skill_roots import skill_path

ROOT = Path(__file__).resolve().parent.parent
SLUG = "precis-repo-dev-start-help"
GENERATED_REL = "docs/repo-dev-start-page.md"
SOUL_REL = "src/precis/data/skills/soul.md"

_docs_index = load_script("docs_index", ROOT / "scripts/docs-index")

#: The gate container has no git repo under /app; the git-derived assertions
#: only mean something in a real checkout (gr645090).
_NO_GIT_REASON = "no git metadata available (gate container); needs a checkout"

#: Index, not manual: one trigger plus its owning page per line. Well
#: under ``test_skill_size.WARN_BYTES`` — a page that needs more than
#: this has started inlining what a linked skill owns.
START_PAGE_MAX_BYTES = 5000

#: The generated fallback is one cold-read file: wrapper + two verbatim
#: embeds (~8 KB today). Drift past this means a source grew, or the
#: generator started inlining what belongs on demand — either way the
#: fallback stops being the cheap first read it exists to be.
GENERATED_MAX_BYTES = 16384


def _text() -> str:
    return skill_path(SLUG).read_text(encoding="utf-8")


def _generated(monkeypatch, root: Path = ROOT) -> str:
    """Render the fallback with the node-cache path pinned (its real
    default is machine state, irrelevant to the page's contract)."""
    monkeypatch.setenv("PRECIS_MEMORY_NODES", "/x/cache/precis/memory-nodes")
    return _docs_index.start_page_text(root)


def _body(path: Path) -> str:
    """Post-front-matter body, stripped — what the embed carries."""
    _fm, body = _docs_index.front_matter(path.read_text(encoding="utf-8"))
    return body.strip()


# ── the served page ────────────────────────────────────────────────────


def test_start_page_exists_at_fixed_slug() -> None:
    """The slug is the contract — local files, the outage fallback and
    any pin name it verbatim."""
    assert skill_path(SLUG).is_file(), (
        f"{SLUG}.md missing — the repo-dev start page lives at a fixed "
        "slug; renaming it breaks the load line, the checkout fallback "
        "and every pin"
    )


def test_frontmatter_id_matches_the_fixed_slug() -> None:
    fm = parse_frontmatter(_text())
    assert fm.id == SLUG, (
        f"id: is {fm.id!r}, expected {SLUG!r} — the served slug comes "
        "from frontmatter and must match the file the checkout loads"
    )
    assert fm.status == "active"


def test_start_page_stays_an_index_not_a_manual() -> None:
    size = skill_path(SLUG).stat().st_size
    assert size <= START_PAGE_MAX_BYTES, (
        f"{SLUG} is {size} bytes (cap {START_PAGE_MAX_BYTES}) — the "
        "start page is a compact index; detail belongs to the skills "
        "and docs it points at"
    )


def test_start_page_resolves_for_pinning() -> None:
    """Delivery rides the cold-start pin path — the page must resolve
    through the same loader the banner uses."""
    result = startup_skills.resolve([SLUG], cap_kb=0)
    assert result.pinned == (SLUG,)
    assert not result.unknown


def test_start_page_carries_the_recall_entry_points() -> None:
    """The soul skill index, rulings hub, vocabulary root and recall
    tag are the live entry points the page routes to — dropping one
    strands recall with no pointer."""
    text = _text()
    for anchor in ('id="soul"', "me489895", "tn559460", "SPACE:repo-dev"):
        assert anchor in text, f"{anchor} no longer appears in {SLUG}"


def test_start_page_names_the_checkout_as_authority() -> None:
    """The precedence rule keeps a stale served copy or memory from
    outranking the tree the session is editing — and names the
    generated file as the checkout-local fallback."""
    text = _text()
    for anchor in (
        "AGENTS.md",
        "docs/conventions/",
        "docs/codebase.md",
        GENERATED_REL,
    ):
        assert anchor in text, f"{anchor!r} no longer appears in {SLUG}"


def test_start_page_links_resolve_as_skill_files() -> None:
    """Every ``[[slug]]`` the page routes to exists as a checkout file —
    the fallback's "skill bodies are the served bodies" claim only
    holds while every linked slug maps 1:1 onto
    ``src/precis/data/skills/`` (synth slugs like ``precis-help`` live
    only in the runtime, so links to those would mislead offline)."""
    for slug in re.findall(r"\[\[([^\]]+)\]\]", _text()):
        assert skill_path(slug).is_file(), (
            f"{SLUG} links [[{slug}]] which has no skill file — the "
            "offline fallback cannot serve it"
        )


# ── the generated, revisioned fallback ─────────────────────────────────


def test_fallback_lands_at_a_fixed_gitignored_path() -> None:
    """The load line (landing in the authorized lane) names one stable
    path; the file must stay gitignored like the other generated views
    and registered in the generated-file allowlists."""
    assert _docs_index.START_PAGE_TARGET == GENERATED_REL
    assert GENERATED_REL in (ROOT / ".gitignore").read_text(encoding="utf-8"), (
        f"{GENERATED_REL} missing from .gitignore — a generated file "
        "must never be committed"
    )
    for test_file in (
        "tests/test_convention_home_paths.py",
        "tests/test_doc_pointers.py",
    ):
        assert GENERATED_REL in (ROOT / test_file).read_text(encoding="utf-8"), (
            f"{GENERATED_REL} missing from the generated-file list in "
            f"{test_file} — doc sweeps would flag a transient render"
        )


def test_fallback_embeds_sources_verbatim(monkeypatch) -> None:
    """No maintained copy: each embedded section is byte-identical to
    its named source's body — the gripe's "generated from the same
    canonical source" half."""
    text = _generated(monkeypatch)
    for rel in _docs_index.START_PAGE_SOURCES:
        body = _body(ROOT / rel)
        assert f"## `{rel}` (file rev " in text, (
            f"{rel} section missing from the fallback"
        )
        assert f"\n\n{body}\n" in text, (
            f"{rel} is not embedded verbatim — the fallback must never "
            "carry a hand-maintained copy"
        )


def test_fallback_is_revisioned(monkeypatch, tmp_path: Path) -> None:
    """Each embed's sha8 plus a sources-as-of stamp and the tree's head
    — a reader can date the mirror, and a stale render is detectable by
    hashing the source."""
    if not _docs_index._git(ROOT, "rev-parse", "--short", "HEAD"):
        pytest.skip(_NO_GIT_REASON)
    text = _generated(monkeypatch)
    for rel in _docs_index.START_PAGE_SOURCES:
        rev = hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()[:8]
        assert f"{rel} {rev}" in text
    assert re.search(r"sources as of \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", text)
    assert re.search(r"head [0-9a-f?]{4,}", text)
    # The stamp rides source mtime, not the wall clock: identical inputs
    # regenerate identically (no per-session churn).
    assert _generated(monkeypatch) == text
    # A changed source flips its rev — the trailer is load-bearing.
    rel = _docs_index.START_PAGE_SOURCES[0]
    staged = tmp_path / rel
    staged.parent.mkdir(parents=True)
    staged.write_text(
        (ROOT / rel).read_text(encoding="utf-8") + "\nextra\n", encoding="utf-8"
    )
    (tmp_path / SOUL_REL).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / SOUL_REL).write_text(
        (ROOT / SOUL_REL).read_text(encoding="utf-8"), encoding="utf-8"
    )
    assert _generated(monkeypatch, tmp_path) != text


def test_fallback_delivers_per_tree_not_per_parent(monkeypatch, tmp_path) -> None:
    """Cold parent/child delivery: each worktree renders from ITS
    checkout — a child on an older/different branch embeds its own
    start page, preserving the local checkout's authority."""
    rel = _docs_index.START_PAGE_SOURCES[0]
    child_text = "---\nid: x\n---\n# child start page\n\nchild body\n"
    staged = tmp_path / rel
    staged.parent.mkdir(parents=True)
    staged.write_text(child_text, encoding="utf-8")
    soul = tmp_path / SOUL_REL
    soul.parent.mkdir(parents=True, exist_ok=True)
    soul.write_text((ROOT / SOUL_REL).read_text(encoding="utf-8"), encoding="utf-8")
    out = _generated(monkeypatch, tmp_path)
    assert "child body" in out
    assert _body(ROOT / rel) not in out


def test_fallback_carries_the_compact_soul_index(monkeypatch) -> None:
    """ "The full compact index at startup" — the soul entry tables ride
    inside the fallback; the 18 entry bodies stay selective fetches."""
    text = _generated(monkeypatch)
    for anchor in ("### Role indexes", "### Principles", "[[soul-engineer]]"):
        assert anchor in text, f"{anchor} missing — the soul index embed broke"


def test_fallback_outage_section_names_the_real_paths(monkeypatch) -> None:
    """Missing-MCP startup: the section exists unconditionally and
    points at the node cache, the shared ``outage-memory.md`` and the
    restore runbook — pointers to scratch, never fabricated bodies."""
    text = _generated(monkeypatch)
    assert "## If the precis MCP or the memory graph is unreachable" in text
    for anchor in (
        "src/precis/data/skills/<slug>.md",
        "/x/cache/precis/memory-nodes",
        "_sections.tsv",
        "outage-memory.md",
        "docs/runbooks/memory-restore.md",
        "dated mirror",
    ):
        assert anchor in text, f"{anchor!r} missing from the outage section"


def test_shared_memory_dir_matches_the_hook_derivation() -> None:
    """The shared bootstrap address has exactly one derivation —
    ``--git-common-dir`` → ``~/.claude/projects/<escaped>/memory`` —
    used identically by the hook, memory-lint and the fallback. A
    worktree resolves to the main checkout, so parent and child share
    the address."""
    common = _docs_index._git(
        ROOT, "rev-parse", "--path-format=absolute", "--git-common-dir"
    )
    if not common:
        pytest.skip(_NO_GIT_REASON)
    expect = str(
        Path.home()
        / ".claude"
        / "projects"
        / str(Path(common).parent).replace("/", "-")
        / "memory"
    )
    assert _docs_index.shared_memory_dir(ROOT) == expect
    # The main checkout's address, not this worktree's: common-dir
    # resolves through .git/worktrees/<name> to the primary .git.
    assert "worktrees" not in expect
    for script in ("scripts/hooks/session-start-memory.sh", "scripts/memory-lint"):
        src = (ROOT / script).read_text(encoding="utf-8")
        assert "--git-common-dir" in src and "//\\//-" in src, (
            f"{script} no longer derives the shared memory dir the same "
            "way — the fallback, hook and lint would disagree on the address"
        )


def test_fallback_stays_a_cheap_first_read(monkeypatch) -> None:
    """Actual retrieval cost: the whole fallback is one bounded read —
    well under a whole-skill serve — not a growing manual."""
    size = len(_generated(monkeypatch).encode("utf-8"))
    assert size <= GENERATED_MAX_BYTES, (
        f"fallback is {size} bytes (cap {GENERATED_MAX_BYTES}) — it "
        "exists to be the cheap cold read; growth belongs to the skills"
    )
