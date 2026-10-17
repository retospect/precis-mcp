"""file-mirror in-scope 1: ``PRECIS_ROOTS`` multi-root file kinds.

A mirrored root is addressed as ``<alias>/<flattened rel-path>``; a root
flagged ``ro`` refuses put/edit/delete with ``Unsupported`` naming it, and
every ref under it carries ``mirror:<alias>`` + ``SPACE:repo-dev``
(``docs/backlog/file-mirror.md``).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from precis.dispatch import Hub, boot
from precis.embedder import MockEmbedder
from precis.errors import Unsupported
from precis.handlers._roots import MirrorRoot, parse_mirror_roots
from precis.handlers.markdown import MarkdownHandler
from precis.store import Store
from precis.utils.link_target import parse_link_target


def _write(root: Path, rel: str, content: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


@pytest.fixture
def roots(tmp_path: Path) -> dict[str, Path]:
    out = {name: tmp_path / name for name in ("ws", "skills", "memory")}
    for p in out.values():
        p.mkdir()
    _write(out["ws"], "notes.md", "# Notes\n\nworkspace body\n")
    _write(
        out["skills"],
        "precis-relations.md",
        "---\nid: precis-relations\n---\n\n# Relations\n\nrelated-to, part-of\n",
    )
    _write(out["memory"], "MEMORY.md", "# Memory\n\n## Gotchas\n\n- [A](a.md)\n")
    _write(out["memory"], "work/foo.md", "# Foo\n\n## Detail\n\nfoo body\n")
    return out


@pytest.fixture
def handler(hub: Hub, roots: dict[str, Path]) -> MarkdownHandler:
    mirrors = {
        "skills": MirrorRoot(path=roots["skills"].resolve(), ro=True),
        "memory": MirrorRoot(path=roots["memory"].resolve(), ro=True),
    }
    return MarkdownHandler(hub=hub, root=roots["ws"], mirrors=mirrors)


def test_parse_mirror_roots_reads_ro_flag(roots: dict[str, Path]) -> None:
    raw = f"skills:{roots['skills']}:ro, memory:{roots['memory']},bad,x:/nope/nowhere"
    parsed = parse_mirror_roots(raw)
    assert parsed == {
        "skills": MirrorRoot(path=roots["skills"].resolve(), ro=True),
        "memory": MirrorRoot(path=roots["memory"].resolve(), ro=False),
    }


def test_parse_mirror_roots_rejects_non_slug_alias(roots: dict[str, Path]) -> None:
    assert parse_mirror_roots(f"Bad Alias:{roots['skills']}") == {}


def test_mirrored_refs_render_under_alias(handler: MarkdownHandler) -> None:
    assert "Relations" in handler.get(id="skills/precis-relations").body
    assert "Memory" in handler.get(id="memory/MEMORY").body
    # Path form keeps the existing ``--`` flattening inside the alias.
    assert "Foo" in handler.get(id="memory/work/foo.md").body
    assert "Detail" in handler.get(id="memory/work--foo~detail").body
    # Workspace slugs are unchanged.
    assert "path:        notes.md" in handler.get(id="notes").body


def test_mirrored_ref_slug_and_tags(handler: MarkdownHandler, store: Store) -> None:
    handler.get(id="memory/work--foo")
    ref = store.get_ref(kind="markdown", id="memory/work--foo")
    assert ref is not None
    tags = {str(t) for t in store.tags_for(ref.id)}
    assert {"mirror:memory", "SPACE:repo-dev"} <= tags
    assert "workspace" not in tags


@pytest.mark.parametrize(
    "call",
    [
        lambda h: h.put(id="skills/new-skill", text="# x\n", mode="create"),
        lambda h: h.edit(id="skills/precis-relations", mode="append", text="more"),
        lambda h: h.edit(
            id="memory/MEMORY", mode="find-replace", find="Memory", text="M"
        ),
        lambda h: h.delete(
            id="memory/work--foo", confirm="delete-file-memory/work--foo"
        ),
    ],
)
def test_ro_root_refuses_writes(
    handler: MarkdownHandler, call: Callable[[MarkdownHandler], Any]
) -> None:
    with pytest.raises(Unsupported, match="read-only"):
        call(handler)


def test_listing_includes_mirrored_slugs(handler: MarkdownHandler) -> None:
    body = handler.get().body
    assert "skills/precis-relations" in body
    assert "memory/work--foo" in body


def test_link_target_reaches_mirrored_section(
    handler: MarkdownHandler, store: Store
) -> None:
    handler.get(id="memory/work--foo")
    target = parse_link_target("markdown:memory/work--foo~detail", store=store)
    ref = store.get_ref(kind="markdown", id="memory/work--foo")
    assert ref is not None and target.ref_id == ref.id


def test_boot_registers_mirrors_from_precis_roots(
    store: Store, roots: dict[str, Path]
) -> None:
    hub = boot(
        store=store,
        embedder=MockEmbedder(dim=store.embedding_dim()),
        precis_root=str(roots["ws"]),
        precis_roots=f"skills:{roots['skills']}:ro",
    )
    md = hub.handlers["markdown"]
    assert "Relations" in md.get(id="skills/precis-relations").body


def test_boot_takes_workspace_alias_from_precis_roots(
    store: Store, roots: dict[str, Path]
) -> None:
    hub = boot(
        store=store,
        embedder=MockEmbedder(dim=store.embedding_dim()),
        precis_roots=f"workspace:{roots['ws']},skills:{roots['skills']}:ro",
    )
    md = hub.handlers["markdown"]
    assert "path:        notes.md" in md.get(id="notes").body
    assert "Relations" in md.get(id="skills/precis-relations").body


def test_ro_flag_follows_the_kept_entry(roots: dict[str, Path], tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    parsed = parse_mirror_roots(f"a:{missing},a:{roots['skills']}:ro")
    assert parsed == {"a": MirrorRoot(path=roots["skills"].resolve(), ro=True)}
    parsed = parse_mirror_roots(f"a:{missing}:RO,a:{roots['skills']}")
    assert parsed == {"a": MirrorRoot(path=roots["skills"].resolve(), ro=False)}


def test_ro_workspace_alias_is_not_mounted_writable(
    store: Store, roots: dict[str, Path]
) -> None:
    hub = boot(
        store=store,
        embedder=MockEmbedder(dim=store.embedding_dim()),
        precis_roots=f"workspace:{roots['ws']}:ro",
    )
    assert "markdown" not in hub.handlers


def test_mirror_nested_in_workspace_refuses_flat_slug_writes(
    hub: Hub, roots: dict[str, Path]
) -> None:
    nested = roots["ws"] / "docs"
    _write(nested, "x.md", "# X\n")
    mirrors = {"docs": MirrorRoot(path=nested.resolve(), ro=True)}
    h = MarkdownHandler(hub=hub, root=roots["ws"], mirrors=mirrors)
    with pytest.raises(Unsupported, match="read-only"):
        h.edit(id="docs--x", mode="append", text="more")
    with pytest.raises(Unsupported, match="read-only"):
        h.put(id="docs--y", text="# y\n", mode="create")
