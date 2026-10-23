"""``thread:<slug>`` on gripes — gripe system batch F slice 5.

A gripe names its owning thread with the open tag ``thread:<slug>``; the
slug must be a ``docs/backlog/threads/<slug>.md`` file (checked at put and
tag, refused with the list). The create ack asks for it; scripts/docs-index
renders each thread's tagged gripes into PRIORITIES.md.
"""

from __future__ import annotations

import runpy
from pathlib import Path
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.gripe import GripeHandler
from precis.utils import thread_files

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/docs-index"


@pytest.fixture
def gripe(hub: Hub) -> GripeHandler:
    return GripeHandler(hub=hub)


@pytest.fixture
def threads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "threads"
    d.mkdir()
    for name in ("ship-gate-ci", "pcb-platform", "README", "INDEX", "PRIORITIES"):
        (d / f"{name}.md").write_text("# x\n", encoding="utf-8")
    monkeypatch.setenv("PRECIS_THREADS_DIR", str(d))
    return d


def _new(gripe: GripeHandler, **kw: Any) -> tuple[int, str]:
    body = gripe.put(text="thread tag fixture", **kw).body
    return int(gripe.store.list_refs(kind="gripe", limit=1)[0].id), body


def _tags(gripe: GripeHandler, gid: int) -> set[str]:
    return {str(t) for t in gripe.store.tags_for(gid)}


def test_slugs_are_thread_files_without_the_index_files(threads: Path) -> None:
    assert thread_files.thread_slugs() == frozenset({"ship-gate-ci", "pcb-platform"})
    assert thread_files.thread_slugs(threads / "absent") is None


def test_the_repo_thread_dir_resolves_from_the_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PRECIS_THREADS_DIR", raising=False)
    slugs = thread_files.thread_slugs()
    assert slugs is not None and "ship-gate-ci" in slugs


def test_tag_with_a_known_thread_lands(gripe: GripeHandler, threads: Path) -> None:
    gid, _ = _new(gripe)
    gripe.tag(id=gid, add=["thread:ship-gate-ci"])
    assert "thread:ship-gate-ci" in _tags(gripe, gid)


def test_unknown_thread_is_refused_with_the_list(
    gripe: GripeHandler, threads: Path
) -> None:
    gid, _ = _new(gripe)
    with pytest.raises(BadInput) as exc:
        gripe.tag(id=gid, add=["thread:shipgate"])
    assert "unknown thread 'shipgate'" in str(exc.value)
    assert exc.value.options == ["pcb-platform", "ship-gate-ci"]
    assert "thread:shipgate" not in _tags(gripe, gid)


def test_batch_tag_checks_the_thread_too(gripe: GripeHandler, threads: Path) -> None:
    a, _ = _new(gripe)
    b, _ = _new(gripe)
    with pytest.raises(BadInput):
        gripe.tag(id=[a, b], add=["thread:nope"])


def test_put_with_an_unknown_thread_files_nothing(
    gripe: GripeHandler, threads: Path
) -> None:
    before = gripe.store.list_refs(kind="gripe", limit=1)
    with pytest.raises(BadInput):
        gripe.put(text="should not file", tags=["thread:nope"])
    assert gripe.store.list_refs(kind="gripe", limit=1) == before


def test_create_ack_asks_for_the_thread_unless_given(
    gripe: GripeHandler, threads: Path
) -> None:
    gid, body = _new(gripe)
    assert f"tag(kind='gripe', id={gid}, add=['thread:<slug>'])" in body
    gid2, body2 = _new(gripe, tags=["thread:pcb-platform"])
    assert "thread:<slug>" not in body2
    assert "thread:pcb-platform" in _tags(gripe, gid2)


def test_put_with_a_backlog_item_slug_gets_the_put_door(
    gripe: GripeHandler, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # gr503548: agents tag docs/backlog/<slug>.md *item* names, not
    # threads/*.md threads. The refusal says so, points at the link
    # door, and corrects the put — not a tag() on an id never minted.
    d = tmp_path / "docs" / "backlog"
    (d / "threads").mkdir(parents=True)
    (d / "threads" / "knowledge-mesh.md").write_text("# x\n", encoding="utf-8")
    (d / "graph-gardener.md").write_text("# x\n", encoding="utf-8")
    monkeypatch.setenv("PRECIS_THREADS_DIR", str(d / "threads"))
    with pytest.raises(BadInput) as exc:
        gripe.put(text="should not file", tags=["thread:graph-gardener"])
    assert "backlog item" in str(exc.value)
    assert "md:main/docs/backlog/graph-gardener.md" in str(exc.value)
    assert isinstance(exc.value.next, str) and exc.value.next.startswith(
        "put(kind='gripe'"
    )
    assert "knowledge-mesh" in (exc.value.options or ())


def test_put_with_a_clipped_backlog_item_slug_gets_the_fuzzy_item_door(
    gripe: GripeHandler, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # gr506766: 'graph-gardener' failed with the bare refusal — the slug is
    # a *clipped* docs/backlog item name ('graph-gardener-value.md'), not an
    # exact one, and no thread slug sat near enough to list. The refusal
    # now fuzzy-matches the item and still names nearest threads.
    d = tmp_path / "docs" / "backlog"
    (d / "threads").mkdir(parents=True)
    (d / "threads" / "graph-memory-consumers.md").write_text("# x\n", encoding="utf-8")
    (d / "graph-gardener-value.md").write_text("# x\n", encoding="utf-8")
    monkeypatch.setenv("PRECIS_THREADS_DIR", str(d / "threads"))
    with pytest.raises(BadInput) as exc:
        gripe.put(text="should not file", tags=["thread:graph-gardener"])
    assert "backlog item" in str(exc.value)
    assert "md:main/docs/backlog/graph-gardener-value.md" in str(exc.value)
    assert "nearest: graph-memory-consumers" in str(exc.value)


def test_unknown_thread_with_no_close_match_still_lists_nearest(
    gripe: GripeHandler, threads: Path
) -> None:
    # gr506766: below the 0.6 cutoff the refusal used to show no slugs at
    # all — the agent had nothing to retry with. Now the top candidates
    # always render.
    with pytest.raises(BadInput) as exc:
        gripe.put(text="should not file", tags=["thread:zzz"])
    assert "nearest: " in str(exc.value)
    assert isinstance(exc.value.next, str) and "thread:" in exc.value.next


def test_put_with_an_unknown_thread_corrects_the_put_with_nearest(
    gripe: GripeHandler, threads: Path
) -> None:
    with pytest.raises(BadInput) as exc:
        gripe.put(text="should not file", tags=["thread:ship-gate"])
    assert "nearest: ship-gate-ci" in str(exc.value)
    assert isinstance(exc.value.next, str) and exc.value.next.startswith(
        "put(kind='gripe'"
    )
    assert "thread:ship-gate-ci" in exc.value.next


def test_tag_with_an_unknown_thread_keeps_the_tag_door(
    gripe: GripeHandler, threads: Path
) -> None:
    gid, _ = _new(gripe)
    with pytest.raises(BadInput) as exc:
        gripe.tag(id=gid, add=["thread:ship-gate"])
    assert "nearest: ship-gate-ci" in str(exc.value)
    assert isinstance(exc.value.next, str) and exc.value.next.startswith(
        f"tag(kind='gripe', id={gid}"
    )


def test_no_thread_files_means_unchecked_and_said(
    gripe: GripeHandler, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_THREADS_DIR", str(tmp_path / "absent"))
    gid, _ = _new(gripe)
    resp = gripe.tag(id=gid, add=["thread:anything"])
    assert "thread tag not checked" in resp.body
    assert "thread:anything" in _tags(gripe, gid)


# ── scripts/docs-index ────────────────────────────────────────────────────


def _script() -> dict[str, Any]:
    return runpy.run_path(str(SCRIPT))


def test_docs_index_gripe_read_is_off_by_switch_and_under_ci(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ns = _script()
    monkeypatch.setenv("PRECIS_DOCS_INDEX_GRIPES", "0")
    assert ns["thread_gripes"](ROOT) == "off (PRECIS_DOCS_INDEX_GRIPES=0)"
    monkeypatch.delenv("PRECIS_DOCS_INDEX_GRIPES")
    monkeypatch.setenv("CI", "true")
    assert ns["thread_gripes"](ROOT) == "off under CI"


def test_docs_index_failed_prod_read_says_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PRECIS_DOCS_INDEX_GRIPES", raising=False)
    monkeypatch.delenv("CI", raising=False)
    fake = tmp_path / "scripts" / "prod-psql"
    fake.parent.mkdir()
    fake.write_text(
        "#!/bin/sh\necho 'no route to prod' >&2\nexit 2\n", encoding="utf-8"
    )
    fake.chmod(0o755)
    got = _script()["thread_gripes"](tmp_path)
    assert got == "unavailable (prod-psql exit 2: no route to prod)"


def test_docs_index_renders_each_thread_and_strays(tmp_path: Path) -> None:
    d = tmp_path / "docs/backlog/threads"
    d.mkdir(parents=True)
    for name in ("alpha", "beta", "README"):
        (d / f"{name}.md").write_text("# x\n", encoding="utf-8")
    render = _script()["render_thread_gripes"]
    rows = render(
        tmp_path, {"alpha": ["gr7 (P3) a", "gr9 (P5) b"], "gone": ["gr1 (P5) c"]}
    )
    assert "- **alpha** (2): gr7 (P3) a; gr9 (P5) b" in rows
    assert "- **beta** (0): none" in rows
    assert "- tagged with no thread file: gone" in rows
    assert render(tmp_path, "off under CI")[-1] == "Gripe list off under CI."
