"""Tests for python-symbol anchor refs (gr702978).

A ``python:<dotted.qualname>`` mention in a note or draft mints a lazy
anchor ``refs(kind='python', slug=<qualname>)`` on first mention
(validated against the live code index), the mention edge lands on that
anchor, and get/eye show the inbound notes. A linked symbol that drops
out of the index renders a "gone" stub instead of a bare NotFound.

The grammar/mint/handler-rendering tests are DB-free (a tiny repo in
``tmp_path`` plus duck-typed fakes where a store is needed); the store
and end-to-end mention tests use the ``store`` fixture.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.python import PythonHandler
from precis.utils import mentions

# ---------------------------------------------------------------------------
# Grammar
# ---------------------------------------------------------------------------


class TestQualnameGrammar:
    def test_dotted_mention_extracted(self) -> None:
        body = (
            "see python:precis.handlers._memory_link_receipt.MentionSync for the sync"
        )
        assert mentions.extract_python_qualnames(body) == [
            "precis.handlers._memory_link_receipt.MentionSync"
        ]

    def test_requires_at_least_one_dot(self) -> None:
        assert mentions.extract_python_qualnames("python:logging is stdlib") == []
        assert mentions.extract_python_qualnames("python:word") == []

    def test_dedup_appearance_order(self) -> None:
        body = "python:pkg.b then python:pkg.a and python:pkg.b again"
        assert mentions.extract_python_qualnames(body) == ["pkg.b", "pkg.a"]

    def test_module_mention_resolves_too(self) -> None:
        # A module qualname is a symbol in the index (the module symbol).
        assert mentions.extract_python_qualnames("python:pkg.runner runs it") == [
            "pkg.runner"
        ]

    def test_trailing_punctuation_not_captured(self) -> None:
        assert mentions.extract_python_qualnames("(python:pkg.mod.Sym).") == [
            "pkg.mod.Sym"
        ]

    def test_extract_handles_pushes_full_qualname(self) -> None:
        body = "see python:pkg.mod.Sym and memory:42"
        handles = mentions.extract_handles(body)
        assert ("python", "pkg.mod.Sym", None) in handles
        # No partial first-segment claim from the generic REF_PATTERN pass.
        assert ("python", "pkg", None) not in handles
        assert ("memory", "42", None) in handles

    def test_extract_handles_python_single_word_dropped(self) -> None:
        # No anchor can exist for a dotless slug; the mention stays prose.
        assert mentions.extract_handles("python:word") == []


# ---------------------------------------------------------------------------
# mint_python_mentions (duck-typed store + handler, no DB)
# ---------------------------------------------------------------------------


class _FakeStore:
    def __init__(self) -> None:
        self.minted: list[tuple[str, str | None, str | None]] = []

    def ensure_python_ref(
        self,
        qualname: str,
        *,
        alias: str | None = None,
        file: str | None = None,
    ) -> int:
        self.minted.append((qualname, alias, file))
        return 1


class _FakePyHandler:
    def __init__(self, known: dict[str, tuple[str, str]]) -> None:
        self.known = known

    def symbol_anchor(self, qualname: str) -> tuple[str, str] | None:
        return self.known.get(qualname)


class TestMintPythonMentions:
    def test_mints_only_validated_qualnames(self) -> None:
        store = _FakeStore()
        handler = _FakePyHandler({"pkg.mod.Sym": ("r", "pkg/mod.py")})
        mentions.mint_python_mentions(
            store, "python:pkg.mod.Sym and python:pkg.ghost.Nope", handler
        )
        assert store.minted == [("pkg.mod.Sym", "r", "pkg/mod.py")]

    def test_no_handler_mints_nothing(self) -> None:
        store = _FakeStore()
        mentions.mint_python_mentions(store, "python:pkg.mod.Sym", None)
        assert store.minted == []

    def test_store_without_ensure_is_a_noop(self) -> None:
        mentions.mint_python_mentions(object(), "python:pkg.mod.Sym", object())

    def test_handler_failure_skips_that_mention(self) -> None:
        class _Boom:
            def symbol_anchor(self, qualname: str) -> None:
                raise RuntimeError("index down")

        store = _FakeStore()
        mentions.mint_python_mentions(store, "python:pkg.mod.Sym", _Boom())
        assert store.minted == []


# ---------------------------------------------------------------------------
# Handler-side anchor machinery (tiny repo, duck-typed store)
# ---------------------------------------------------------------------------


def _write(repo: Path, rel: str, content: str) -> Path:
    f = repo / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
    return f


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _write(tmp_path, "pkg/__init__.py", '"""pkg — the sample package."""\n')
    _write(
        tmp_path,
        "pkg/runner.py",
        '''
        """The runner module."""

        def target(x: int) -> int:
            """Run the thing."""
            return x + 1
        ''',
    )
    return tmp_path


@pytest.fixture
def handler(repo: Path) -> PythonHandler:
    return PythonHandler(hub=Hub(), roots={"r": repo})


class _FakeRef:
    def __init__(
        self, ref_id: int, kind: str, title: str, meta: dict[str, Any] | None = None
    ) -> None:
        self.id = ref_id
        self.ref_id = ref_id
        self.kind = kind
        self.title = title
        self.public_id = title
        self.meta = meta or {}


class _FakeLink:
    def __init__(self, src_ref_id: int) -> None:
        self.src_ref_id = src_ref_id


class _AnchorStore:
    """Duck-typed stand-in for the store bits the anchor machinery reads."""

    def __init__(self, anchor: _FakeRef | None, citing: list[_FakeRef]) -> None:
        self._anchor = anchor
        self._citing = citing

    def python_symbol_ref_id(self, qualname: str) -> int | None:
        if self._anchor is None:
            return None
        return int(self._anchor.ref_id)

    def links_for(self, ref_id: int, *, direction: str = "both") -> list[_FakeLink]:
        return [_FakeLink(r.ref_id) for r in self._citing]

    def fetch_refs_by_ids(
        self, ref_ids: Any, *, include_deleted: bool = True
    ) -> dict[int, _FakeRef]:
        return {r.ref_id: r for r in self._citing}

    def get_ref(self, *, kind: str, id: Any, include_deleted: bool = False) -> Any:
        if kind == "python" and self._anchor is not None and id == self._anchor.title:
            return self._anchor
        return None


def _bound(handler: PythonHandler, store: _AnchorStore) -> PythonHandler:
    """Bind a duck-typed store the way a booted hub would."""
    setattr(handler, "store", store)
    return handler


class TestSymbolAnchor:
    def test_resolves_known_qualname(self, handler: PythonHandler) -> None:
        assert handler.symbol_anchor("pkg.runner.target") == ("r", "pkg/runner.py")

    def test_module_qualname(self, handler: PythonHandler) -> None:
        assert handler.symbol_anchor("pkg.runner") == ("r", "pkg/runner.py")

    def test_unknown_qualname(self, handler: PythonHandler) -> None:
        assert handler.symbol_anchor("pkg.runner.ghost") is None
        assert handler.symbol_anchor("other.mod.f") is None


class TestInboundNotesRendering:
    def test_get_shows_notes_section(self, handler: PythonHandler, repo: Path) -> None:
        anchor = _FakeRef(7, "python", "pkg.runner.target", {"alias": "r"})
        citing = _FakeRef(9, "memory", "MentionSync keeps mentions honest")
        _bound(handler, _AnchorStore(anchor, [citing]))
        body = handler.get(id="r::pkg.runner.target").body
        assert "— notes citing this symbol —" in body
        assert "me9" in body
        assert "MentionSync keeps mentions honest" in body

    def test_get_without_anchor_has_no_notes_section(
        self, handler: PythonHandler
    ) -> None:
        _bound(handler, _AnchorStore(None, []))
        body = handler.get(id="r::pkg.runner.target").body
        assert "notes citing this symbol" not in body

    def test_get_with_storeless_hub_is_silent(self, handler: PythonHandler) -> None:
        body = handler.get(id="r::pkg.runner.target").body
        assert "notes citing this symbol" not in body

    def test_eye_shows_notes_at_hop1(self, handler: PythonHandler) -> None:
        anchor = _FakeRef(7, "python", "pkg.runner.target")
        citing = _FakeRef(9, "memory", "a note")
        _bound(handler, _AnchorStore(anchor, [citing]))
        body = handler.eye(id="r::pkg.runner.target", extent="fisheye+1hop").body
        assert "— notes citing this symbol —" in body
        assert "me9" in body

    def test_eye_summary_rung_omits_notes(self, handler: PythonHandler) -> None:
        anchor = _FakeRef(7, "python", "pkg.runner.target")
        citing = _FakeRef(9, "memory", "a note")
        _bound(handler, _AnchorStore(anchor, [citing]))
        body = handler.eye(id="r::pkg.runner.target", extent="summary").body
        assert "notes citing this symbol" not in body

    def test_rows_cap_with_overflow(self, handler: PythonHandler) -> None:
        anchor = _FakeRef(7, "python", "pkg.runner.target")
        citing = [_FakeRef(100 + i, "memory", f"note {i}") for i in range(12)]
        _bound(handler, _AnchorStore(anchor, citing))
        body = handler.get(id="r::pkg.runner.target").body
        assert "… +4 more" in body
        assert "note 11" not in body


class TestGoneStub:
    def test_gone_symbol_renders_stub(self, handler: PythonHandler) -> None:
        anchor = _FakeRef(
            7, "python", "pkg.runner.ghost", {"alias": "r", "file": "pkg/runner.py"}
        )
        citing = _FakeRef(9, "memory", "why the ghost mattered")
        _bound(handler, _AnchorStore(anchor, [citing]))
        body = handler.get(id="r::pkg.runner.ghost").body
        assert body.startswith("# gone: r::pkg.runner.ghost")
        assert "python:pkg.runner.ghost" in body
        assert "py7" not in body
        assert "why the ghost mattered" in body
        assert "last seen as r/pkg/runner.py" in body

    def test_gone_symbol_eye_renders_stub(self, handler: PythonHandler) -> None:
        anchor = _FakeRef(7, "python", "pkg.runner.ghost")
        _bound(handler, _AnchorStore(anchor, []))
        body = handler.eye(id="r::pkg.runner.ghost", extent="fisheye+1hop").body
        assert body.startswith("# gone: r::pkg.runner.ghost")

    def test_unknown_symbol_without_anchor_still_notfound(
        self, handler: PythonHandler
    ) -> None:
        _bound(handler, _AnchorStore(None, []))
        with pytest.raises(NotFound):
            handler.get(id="r::pkg.runner.ghost")
        with pytest.raises(NotFound):
            handler.eye(id="r::pkg.runner.ghost", extent="fisheye+1hop")

    def test_stub_carries_rename_hints(self, handler: PythonHandler) -> None:
        anchor = _FakeRef(7, "python", "pkg.runner.targets")
        _bound(handler, _AnchorStore(anchor, []))
        body = handler.get(id="r::pkg.runner.targets").body
        assert "did you mean" in body
        assert "r::pkg.runner.target" in body


# ---------------------------------------------------------------------------
# Store + end-to-end (real DB)
# ---------------------------------------------------------------------------


@pytest.fixture
def py_kind(store: Any) -> None:
    store.upsert_kinds([PythonHandler.spec])


class TestEnsurePythonRef:
    def test_mint_then_probe(self, store: Any, py_kind: None) -> None:
        rid = store.ensure_python_ref(
            "pkg.runner.target", alias="r", file="pkg/runner.py"
        )
        assert rid > 0
        assert store.ensure_python_ref("pkg.runner.target") == rid
        assert store.python_symbol_ref_id("pkg.runner.target") == rid
        assert store.python_symbol_ref_id("pkg.runner.ghost") is None
        ref = store.get_ref(kind="python", id="pkg.runner.target")
        assert ref is not None
        assert ref.meta.get("alias") == "r"
        assert ref.meta.get("file") == "pkg/runner.py"

    def test_anchor_resolves_as_handle_cite(self, store: Any, py_kind: None) -> None:
        rid = store.ensure_python_ref("pkg.runner.target")
        # The slug registered a cite_key identifier row: the generic
        # kind:id resolution path sees the anchor.
        from precis.utils.mentions import resolve_handle_ref

        assert resolve_handle_ref(store, "pkg.runner.target").id == rid


class TestMentionEndToEnd:
    def test_note_mention_links_anchor(
        self, store: Any, py_kind: None, repo: Path
    ) -> None:
        hub = Hub(store=store)
        py = PythonHandler(hub=hub, roots={"r": repo})
        py._register_with(hub)
        body = "the sync lives in python:pkg.runner.target"
        mentions.mint_python_mentions(store, body, py)
        rid = store.python_symbol_ref_id("pkg.runner.target")
        assert rid is not None
        targets = mentions.resolve_link_targets(store, body)
        assert any(t.dst_ref_id == rid for t in targets)

    def test_unknown_mention_mints_nothing(
        self, store: Any, py_kind: None, repo: Path
    ) -> None:
        hub = Hub(store=store)
        py = PythonHandler(hub=hub, roots={"r": repo})
        mentions.mint_python_mentions(store, "python:pkg.runner.ghost", py)
        assert store.python_symbol_ref_id("pkg.runner.ghost") is None


class TestLinkVerb:
    def test_link_add_edge(self, store: Any, py_kind: None, repo: Path) -> None:
        hub = Hub(store=store)
        py = PythonHandler(hub=hub, roots={"r": repo})
        py._register_with(hub)
        other = store.insert_ref(kind="todo", slug=None, title="follow up")
        resp = py.link(id="r::pkg.runner.target", target=f"td{other.id}")
        assert "+1 link" in resp.body
        rid = store.python_symbol_ref_id("pkg.runner.target")
        # link(kind, id=<symbol>, target=...) makes the symbol the edge
        # source, same as every other kind's link verb.
        outbound = store.links_for(rid, direction="out")
        assert any(l.dst_ref_id == other.id for l in outbound)
        # And the reverse direction is what the inbound-notes section reads.
        store.add_link(src_ref_id=other.id, dst_ref_id=rid, relation="related-to")
        inbound = store.links_for(rid, direction="in")
        assert any(l.src_ref_id == other.id for l in inbound)

    def test_link_unknown_symbol_refuses(
        self, store: Any, py_kind: None, repo: Path
    ) -> None:
        hub = Hub(store=store)
        py = PythonHandler(hub=hub, roots={"r": repo})
        py._register_with(hub)
        other = store.insert_ref(kind="todo", slug=None, title="follow up")
        with pytest.raises(NotFound):
            py.link(id="r::pkg.runner.ghost", target=f"td{other.id}")
        assert store.python_symbol_ref_id("pkg.runner.ghost") is None

    def test_python_target_mints_via_apply_link_ops(
        self, store: Any, py_kind: None, repo: Path
    ) -> None:
        from precis.utils.link_target import mint_lazy_link_target

        hub = Hub(store=store)
        py = PythonHandler(hub=hub, roots={"r": repo})
        hub.register_handler("python", py)
        mint_lazy_link_target("python:pkg.runner.target", store=store, hub=hub)
        assert store.python_symbol_ref_id("pkg.runner.target") is not None
        with pytest.raises(NotFound):
            mint_lazy_link_target("python:pkg.runner.ghost", store=store, hub=hub)

    def test_python_target_alias_prefix_mints_bare_qualname(
        self, store: Any, py_kind: None, repo: Path
    ) -> None:
        # gr743222: search rows print '<alias>::<qualname>' handles, so a
        # link target carrying that prefix must mint and resolve the same
        # bare-qualname anchor the plain target does.
        from precis.utils.link_target import mint_lazy_link_target, parse_link_target

        hub = Hub(store=store)
        py = PythonHandler(hub=hub, roots={"r": repo})
        hub.register_handler("python", py)
        mint_lazy_link_target("python:r::pkg.runner.target", store=store, hub=hub)
        rid = store.python_symbol_ref_id("pkg.runner.target")
        assert rid is not None
        assert (
            parse_link_target("python:r::pkg.runner.target", store=store).ref_id == rid
        )

    def test_python_target_wrong_alias_refuses(
        self, store: Any, py_kind: None, repo: Path
    ) -> None:
        from precis.utils.link_target import mint_lazy_link_target

        hub = Hub(store=store)
        py = PythonHandler(hub=hub, roots={"r": repo})
        hub.register_handler("python", py)
        with pytest.raises(NotFound, match="no python root"):
            mint_lazy_link_target(
                "python:other::pkg.runner.target", store=store, hub=hub
            )
        assert store.python_symbol_ref_id("pkg.runner.target") is None

    def test_memory_link_to_search_printed_python_handle(
        self, store: Any, py_kind: None, repo: Path
    ) -> None:
        # The gr743222 call shape — link(kind='memory', id=<note>, target=
        # 'python:<alias>::<qualname>') — through apply_link_ops, the door
        # every non-python link verb delegates to.
        from precis.utils.link_tag_ops import apply_link_ops

        hub = Hub(store=store)
        py = PythonHandler(hub=hub, roots={"r": repo})
        hub.register_handler("python", py)
        note = store.insert_ref(kind="memory", slug=None, title="the note")
        n_added, n_removed, proposals = apply_link_ops(
            store,
            note.id,
            link="python:r::pkg.runner.target",
            unlink=None,
            rel="derived-from",
            hub=hub,
        )
        assert (n_added, n_removed) == (1, 0)
        rid = store.python_symbol_ref_id("pkg.runner.target")
        assert rid is not None
        outbound = store.links_for(note.id, direction="out")
        assert any(l.dst_ref_id == rid for l in outbound)

    def test_memory_link_two_roots_same_qualname(
        self, store: Any, py_kind: None, repo: Path, tmp_path_factory: Any
    ) -> None:
        # Two roots hold the same qualname: a handle printed for the later
        # root ('z') must validate against z, not the alphabetical first.
        # Goes through MemoryHandler.link (NumericRefHandler._link), the
        # single-memory door. A typo'd alias refuses on unlink as on add.
        from precis.handlers.memory import MemoryHandler

        other = tmp_path_factory.mktemp("other")
        _write(other, "pkg/__init__.py", '"""pkg."""\n')
        _write(other, "pkg/runner.py", "def target(x: int) -> int:\n    return x\n")
        hub = Hub(store=store)
        hub.register_handler(
            "python", PythonHandler(hub=hub, roots={"a": repo, "z": other})
        )
        mem = MemoryHandler(hub=hub)
        note = store.insert_ref(kind="memory", slug=None, title="the note")
        h = note.id
        mem.link(id=h, target="python:z::pkg.runner.target", rel="derived-from")
        rid = store.python_symbol_ref_id("pkg.runner.target")
        assert rid is not None
        assert store.get_ref(kind="python", id="pkg.runner.target").meta["alias"] == "z"
        assert any(
            l.dst_ref_id == rid for l in store.links_for(note.id, direction="out")
        )
        with pytest.raises(NotFound, match="no python root"):
            mem.link(id=h, target="python:typo::pkg.runner.target", mode="remove")
        assert any(
            l.dst_ref_id == rid for l in store.links_for(note.id, direction="out")
        )
        mem.link(id=h, target="python:z::pkg.runner.target", mode="remove")
        assert not store.links_for(note.id, direction="out")

    @pytest.mark.parametrize("kind", ["memory", "rxn"])
    def test_link_rejects_invalid_python_aliases(
        self, store: Any, py_kind: None, repo: Path, kind: str
    ) -> None:
        from precis.handlers.memory import MemoryHandler
        from precis.handlers.rxn import RxnHandler

        hub = Hub(store=store)
        hub.register_handler("python", PythonHandler(hub=hub, roots={"r": repo}))
        if kind == "rxn":
            store.upsert_kinds([RxnHandler.spec])
        handler = RxnHandler(hub=hub) if kind == "rxn" else MemoryHandler(hub=hub)
        slug = "reaction" if kind == "rxn" else None
        ref = store.insert_ref(kind=kind, slug=slug, title="source")
        source = slug if slug is not None else ref.id
        target = "python:r::pkg.runner.target"
        handler.link(id=source, target=target)
        for mode in ("add", "remove"):
            with pytest.raises(BadInput, match="requires a root alias"):
                handler.link(id=source, target="python:::pkg.runner.target", mode=mode)
            with pytest.raises(NotFound, match="no python root"):
                handler.link(
                    id=source, target="python:typo::pkg.runner.target", mode=mode
                )
        assert len(store.links_for(ref.id, direction="out")) == 1
        handler.link(id=source, target=target, mode="remove")
        assert not store.links_for(ref.id, direction="out")
