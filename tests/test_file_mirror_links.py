"""file-mirror in-scope 3-4: ``precis mirror-links`` and the skill tree.

Memory files link by ``[Title](slug.md)`` and ``[[slug]]``; ``MEMORY.md``
bullets are ``part-of`` the ``##`` section they sit under; skills are
``part-of`` their ``parent:`` (or the default rule). Every minted link
carries ``meta.source='mirror'`` so a re-run removes what a file no
longer says (``docs/backlog/file-mirror.md``).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from precis.cli.mirror_links import skill_parents, sync_mirror_links
from precis.dispatch import Hub
from precis.handlers._roots import MirrorRoot
from precis.handlers.markdown import MarkdownHandler
from precis.store import Store
from precis.utils.skill_common import parse_frontmatter

FIXTURE = Path(__file__).parent / "fixtures" / "file_mirror" / "harness"


def _write(root: Path, rel: str, content: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _skill(slug: str, *, kinds: str = "", parent: str = "", body: str = "") -> str:
    fm = [f"id: {slug}", f"title: {slug}"]
    if kinds:
        fm.append(f"kinds: [{kinds}]")
    if parent:
        fm.append(f"parent: {parent}")
    return "---\n" + "\n".join(fm) + f"\n---\n\n# {slug}\n\n{body or slug}\n"


@pytest.fixture
def tree(tmp_path: Path) -> dict[str, Path]:
    ws, memory, skills = tmp_path / "ws", tmp_path / "memory", tmp_path / "skills"
    ws.mkdir()
    shutil.copytree(FIXTURE, memory)
    skills.mkdir()
    _write(skills, "precis-overview.md", _skill("precis-overview"))
    _write(skills, "precis-draft-help.md", _skill("precis-draft-help", kinds="draft"))
    _write(
        skills,
        "precis-draft-figures.md",
        _skill(
            "precis-draft-figures", kinds="draft", body="see [[precis-fisheye-help]]"
        ),
    )
    _write(
        skills,
        "precis-fisheye-help.md",
        _skill(
            "precis-fisheye-help",
            parent="precis-overview",
            body="[[precis-draft-help]]",
        ),
    )
    return {"ws": ws, "memory": memory, "skills": skills}


@pytest.fixture
def handler(hub: Hub, tree: dict[str, Path]) -> MarkdownHandler:
    mirrors = {
        "memory": MirrorRoot(path=tree["memory"].resolve(), ro=True),
        "skills": MirrorRoot(path=tree["skills"].resolve(), ro=True),
    }
    return MarkdownHandler(hub=hub, root=tree["ws"], mirrors=mirrors)


def _out(store: Store, slug: str, relation: str) -> set[str]:
    ref = store.get_ref(kind="markdown", id=slug)
    assert ref is not None, slug
    out: set[str] = set()
    for link in store.links_for(ref.id, direction="out", relation=relation):
        dst = store.get_ref(kind="markdown", id=link.dst_ref_id)
        if dst is None:
            continue
        out.add(
            f"{dst.slug}~{link.dst_ord}" if link.dst_ord is not None else str(dst.slug)
        )
    return out


def test_parent_key_parses() -> None:
    fm = parse_frontmatter(_skill("precis-x", parent="precis-overview"))
    assert fm.parent == "precis-overview"


def test_skill_parent_default_rule() -> None:
    fms = {
        "precis-overview": parse_frontmatter(_skill("precis-overview")),
        "precis-draft-help": parse_frontmatter(
            _skill("precis-draft-help", kinds="draft")
        ),
        "precis-draft-figures": parse_frontmatter(
            _skill("precis-draft-figures", kinds="draft")
        ),
        "precis-loose": parse_frontmatter(_skill("precis-loose", kinds="nokind")),
        "precis-pinned": parse_frontmatter(
            _skill("precis-pinned", kinds="draft", parent="precis-loose")
        ),
    }
    assert skill_parents(fms) == {
        "precis-draft-help": "precis-overview",
        "precis-draft-figures": "precis-draft-help",
        "precis-loose": "precis-overview",
        "precis-pinned": "precis-loose",
    }


def test_memory_links_and_idempotent_removal(
    handler: MarkdownHandler, store: Store, tree: dict[str, Path]
) -> None:
    sync_mirror_links(store, handler)
    src = "memory/ship-gate-red"
    want = {"memory/gate-slots", "memory/flaky-rerun", "memory/migration-numbering"}
    assert _out(store, src, "related-to") == want
    again = sync_mirror_links(store, handler)
    assert (again.added, again.removed) == (0, 0)

    path = tree["memory"] / "ship-gate-red.md"
    text = path.read_text(encoding="utf-8").replace("- [[migration-numbering]]\n", "")
    path.write_text(text, encoding="utf-8")
    report = sync_mirror_links(store, handler)
    assert (report.added, report.removed) == (0, 1)
    assert _out(store, src, "related-to") == want - {"memory/migration-numbering"}


def test_memory_index_bullets_are_part_of_their_section(
    handler: MarkdownHandler, store: Store
) -> None:
    sync_mirror_links(store, handler)
    (parent,) = _out(store, "memory/utc-timestamps", "part-of")
    slug, _, pos = parent.partition("~")
    assert slug == "memory/memory"
    index = store.get_ref(kind="markdown", id="memory/memory")
    assert index is not None
    chunk = store.chunks.get_chunk(index.id, pos=int(pos))
    assert chunk is not None and chunk.text.startswith("## Conventions")


def test_skill_tree_and_cross_refs(handler: MarkdownHandler, store: Store) -> None:
    sync_mirror_links(store, handler)
    assert _out(store, "skills/precis-draft-figures", "part-of") == {
        "skills/precis-draft-help"
    }
    assert _out(store, "skills/precis-draft-figures", "related-to") == {
        "skills/precis-fisheye-help"
    }
    assert _out(store, "skills/precis-fisheye-help", "part-of") == {
        "skills/precis-overview"
    }
    assert _out(store, "skills/precis-overview", "part-of") == set()


def test_fisheye_shows_parent_and_neighbours(
    store: Store, tree: dict[str, Path]
) -> None:
    """AC 4, through the runtime (the eye views live above the handler)."""
    from precis.config import PrecisConfig
    from precis.dispatch import boot
    from precis.embedder import MockEmbedder
    from precis.runtime import PrecisRuntime

    hub = boot(
        store=store,
        embedder=MockEmbedder(dim=store.embedding_dim()),
        precis_root=str(tree["ws"]),
        precis_roots=f"skills:{tree['skills']}:ro",
    )
    runtime = PrecisRuntime(config=PrecisConfig(), hub=hub)
    sync_mirror_links(store, hub.handlers["markdown"])
    body = runtime.dispatch(
        "get",
        {
            "kind": "markdown",
            "id": "skills/precis-fisheye-help",
            "view": "fisheye+1hop",
        },
    )
    assert "precis-overview" in body
    assert "precis-draft-help" in body


def test_dry_run_writes_nothing(handler: MarkdownHandler, store: Store) -> None:
    report = sync_mirror_links(store, handler, dry_run=True)
    assert report.added > 0
    assert store.get_ref(kind="markdown", id="memory/ship-gate-red") is None
    assert store.get_ref(kind="markdown", id="skills/precis-overview") is None


def test_agent_links_survive_and_are_not_claimed(
    handler: MarkdownHandler, store: Store
) -> None:
    handler.ensure_ingested("memory/ship-gate-red")
    handler.ensure_ingested("memory/gate-slots")
    handler.ensure_ingested("memory/squash-audit")
    src = store.get_ref(kind="markdown", id="memory/ship-gate-red")
    same = store.get_ref(kind="markdown", id="memory/gate-slots")
    other = store.get_ref(kind="markdown", id="memory/squash-audit")
    assert src is not None and same is not None and other is not None
    store.add_link(src_ref_id=src.id, dst_ref_id=same.id, relation="related-to")
    store.add_link(src_ref_id=src.id, dst_ref_id=other.id, relation="related-to")
    sync_mirror_links(store, handler)
    sync_mirror_links(store, handler)
    links = {
        link.dst_ref_id: link
        for link in store.links_for(src.id, direction="out", relation="related-to")
    }
    assert other.id in links and same.id in links
    assert (links[same.id].meta or {}).get("source") != "mirror"


def test_removed_file_retires_its_ref(
    handler: MarkdownHandler, store: Store, tree: dict[str, Path]
) -> None:
    sync_mirror_links(store, handler)
    (tree["memory"] / "flaky-rerun.md").unlink()
    sync_mirror_links(store, handler)
    assert store.get_ref(kind="markdown", id="memory/flaky-rerun") is None
    assert "memory/flaky-rerun" not in _out(store, "memory/ship-gate-red", "related-to")


def test_section_heading_match_is_exact(
    handler: MarkdownHandler, store: Store, tree: dict[str, Path]
) -> None:
    index = tree["memory"] / "MEMORY.md"
    text = index.read_text(encoding="utf-8")
    index.write_text(
        "# Index\n\n## Conv\n\n- [x](squash-audit.md)\n\n" + text.split("\n", 1)[1],
        encoding="utf-8",
    )
    sync_mirror_links(store, handler)
    (parent,) = _out(store, "memory/utc-timestamps", "part-of")
    ref = store.get_ref(kind="markdown", id="memory/memory")
    assert ref is not None
    chunk = store.chunks.get_chunk(ref.id, pos=int(parent.partition("~")[2]))
    assert chunk is not None and chunk.text.startswith("## Conventions")


def test_parent_cycles_and_unknown_parents_reroot() -> None:
    fms = {
        "precis-overview": parse_frontmatter(_skill("precis-overview")),
        "precis-a": parse_frontmatter(_skill("precis-a", parent="precis-b")),
        "precis-b": parse_frontmatter(_skill("precis-b", parent="precis-a")),
        "precis-c": parse_frontmatter(_skill("precis-c", parent="precis-nope")),
    }
    assert skill_parents(fms) == {
        "precis-a": "precis-overview",
        "precis-b": "precis-a",
        "precis-c": "precis-overview",
    }
