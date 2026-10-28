"""gr514049 layers 2-3 — the handle lint and the export expansion of
``# <handle> (<why>)`` annotations in workspace code.

The parser accepts the shipped layer-1 form (handle first, gloss
after) and refuses prose that only mentions a handle — the spec's
open question resolved in favour of the live convention
(docs/backlog/workspace-db-references.md).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from precis.errors import BadInput
from precis.provenance import refs as R

TODAY = "2026-10-27Z"


# ── find_annotations ────────────────────────────────────────────────────


def _one(src: str) -> R.Annotation:
    anns = R.find_annotations(src)
    assert len(anns) == 1
    return anns[0]


def test_trailing_annotation_carries_gloss() -> None:
    ann = _one('r = b.part("R1")  # fi349615 (I2C pull-up sizing)\n')
    assert ann.handles == ("fi349615",)
    assert ann.gloss == "(I2C pull-up sizing)"
    assert ann.trailing is True
    assert ann.lineno == 1


def test_standalone_annotation() -> None:
    ann = _one("# pa4474653\nr = 1\n")
    assert ann.handles == ("pa4474653",)
    assert ann.gloss == ""
    assert ann.trailing is False


def test_multi_handle_and_gloss() -> None:
    ann = _one("r = 1  # pc88213 pa4474653 (dropout)\n")
    assert ann.handles == ("pc88213", "pa4474653")
    assert ann.gloss == "(dropout)"


def test_annotation_inside_multiline_call() -> None:
    src = 'r = b.part(\n    "R1", lcsc="C1",\n)  # pn7\n'
    ann = _one(src)
    assert ann.lineno == 3
    assert ann.handles == ("pn7",)


def test_pipe_gloss_separator() -> None:
    ann = _one("r = 1  # pc88213~p12 | AMS1117 dropout 1.1 V\n")
    assert ann.handles == ("pc88213~p12",)
    assert ann.gloss == "AMS1117 dropout 1.1 V"


def test_prose_mentions_are_not_annotations() -> None:
    src = (
        "x = 1  # see gr513665 for history\n"
        "y = 2  # lcsc:C6186\n"
        "z = 3  # ds:ams1117~p12\n"
        "w = 4  # [3] bracket cite\n"
        "v = 5  # pa5x not a handle\n"
        "u = 6  # pa5: trailing colon\n"
    )
    assert R.find_annotations(src) == []


def test_unknown_code_shape_is_still_an_annotation() -> None:
    ann = _one("x = 1  # zz9 (typo'd code — must be flagged)\n")
    assert ann.handles == ("zz9",)


def test_handles_in_strings_are_not_comments() -> None:
    src = 's = "# pa5 inside a string"\ndoc = """# pa6 docstring"""\n'
    assert R.find_annotations(src) == []


def test_generated_forms_are_not_annotations() -> None:
    src = (
        "x = 1  # [2] expanded\n"
        "#| detail line\n"
        "# ==== precis references ====\n"
        "# [1] pa5 | cite | sha256:abc | 2026-01-01Z\n"
        "# ==== /precis references ====\n"
        "y = 2  # (unresolved: pc9)\n"
    )
    assert R.find_annotations(src) == []


# ── lint_references ─────────────────────────────────────────────────────


def _stub(known: set[str]) -> Any:
    return lambda h: object() if h in known else None


def test_lint_lists_unresolved_with_line() -> None:
    src = "a = 1  # pa5\nb = 2  # pa999\nc = 3  # zz7\n"
    issues = R.lint_references(src, resolve=_stub({"pa5"}))
    assert [(i.lineno, i.handle) for i in issues] == [(2, "pa999"), (3, "zz7")]


def test_lint_record_selector_resolves_the_record() -> None:
    """``pa5~p12`` lints against ``pa5`` — the selector is citation
    detail, not part of the address."""
    issues = R.lint_references("x = 1  # pa5~p12\n", resolve=_stub({"pa5"}))
    assert issues == []


def test_lint_chunk_bad_selector_flagged() -> None:
    """``pc10~bogus``: the base is a chunk handle, so the malformed
    selector is the flag — not silently re-based."""
    issues = R.lint_references("x = 1  # pc10~bogus\n", resolve=_stub({"pc10"}))
    assert [i.reason for i in issues] == ["selector"]


def test_lint_real_store(store: Any) -> None:
    ref = store.insert_ref(kind="paper", slug="lintp20", title="t", meta={})
    h = f"pa{ref.id}"
    src = f"x = 1  # {h}\ny = 2  # pa999999999\n"
    issues = R.lint_references(src, resolve=store.resolve_handle)
    assert [(i.lineno, i.handle) for i in issues] == [(2, "pa999999999")]


# ── expand_references / strip_references ────────────────────────────────


class _CiteResolver:
    def __init__(self, cites: dict[str, R.RefCitation | None]) -> None:
        self._cites = cites

    def cite(self, handle: str) -> R.RefCitation | None:
        return self._cites.get(handle)


def _resolver() -> _CiteResolver:
    return _CiteResolver(
        {
            "pa5": R.RefCitation(
                "pa5",
                "paper",
                citation='Smith (2020). "A paper". doi:10.1/x',
                excerpt="the abstract text",
            ),
            "pa7": R.RefCitation("pa7", "paper", citation="Jones (2021)."),
            "gr5": R.RefCitation("gr5", "gripe", internal=True),
        }
    )


def test_expand_block_rewrites_and_appends() -> None:
    src = "x = 1  # pa5 (why)\ny = 2  # pa7\n"
    res = R.expand_references(
        src, resolver=_resolver(), include_excerpts=True, today=TODAY
    )
    out = res.source
    assert "x = 1  # [1] (why)" in out
    assert "y = 2  # [2]" in out
    assert R._BLOCK_OPEN in out and R._BLOCK_CLOSE in out
    assert "# [1] pa5 | Smith (2020)." in out
    assert "sha256:" in out and TODAY in out
    assert "#     > the abstract text" in out
    assert res.report.added == ["pa5", "pa7"]


def test_expand_unresolved_and_internal() -> None:
    src = "x = 1  # pc9 (why)\ny = 2  # gr5\n"
    res = R.expand_references(src, resolver=_resolver(), today=TODAY)
    assert "# (unresolved: pc9) (why)" in res.source
    assert "# (internal ref, not exported)" in res.source
    assert res.report.unresolved == ["pc9"]
    assert res.report.internal == ["gr5"]
    # no references block entries — nothing cited
    assert "pa5" not in res.source


def test_expand_block_round_trip() -> None:
    src = "x = 1  # pa5 (why)\n# pa7 a note\ny = 2\n"
    res = R.expand_references(src, resolver=_resolver(), today=TODAY)
    assert R.strip_references(res.source) == src


def test_expand_is_idempotent() -> None:
    src = "x = 1  # pa5 (why)\ny = 2  # pa7\n"
    once = R.expand_references(src, resolver=_resolver(), today=TODAY).source
    twice = R.expand_references(once, resolver=_resolver(), today=TODAY).source
    assert twice == once


def test_expand_reexport_reports_changed_digest() -> None:
    src = "x = 1  # pa5\n"
    res1 = R.expand_references(src, resolver=_resolver(), today=TODAY)
    cites = _resolver()._cites
    cites["pa5"] = R.RefCitation(
        "pa5", "paper", citation='Smith (2020). "A paper". doi:10.1/x', excerpt="NEW"
    )
    res2 = R.expand_references(
        res1.source,
        resolver=_CiteResolver(cites),
        include_excerpts=True,
        today="2026-10-28Z",
    )
    assert res2.report.changed == ["pa5"]
    assert "# [1] pa5" in res2.source  # numbering kept
    assert "> NEW" in res2.source


def test_expand_no_excerpt_switch() -> None:
    src = "x = 1  # pa5\n"
    res = R.expand_references(
        src, resolver=_resolver(), include_excerpts=False, today=TODAY
    )
    assert "# [1] pa5 | Smith (2020)." in res.source
    assert "> the abstract text" not in res.source
    assert "sha256:" in res.source


def test_expand_inline_mode() -> None:
    src = "def f():\n    x = 1  # pa5 (why)\n    return x\n"
    res = R.expand_references(
        src, resolver=_resolver(), mode="inline", include_excerpts=True, today=TODAY
    )
    out = res.source
    assert "    x = 1  # pa5 (why)" in out
    assert '    #| Smith (2020). "A paper". doi:10.1/x | sha256:' in out
    assert '    #| "the abstract text"' in out
    assert R.strip_references(out) == src
    compile(out, "t.py", "exec")


def test_expand_sidecar_mode() -> None:
    src = "x = 1  # pa5\n"
    res = R.expand_references(
        src,
        resolver=_resolver(),
        mode="sidecar",
        include_excerpts=True,
        today=TODAY,
    )
    assert "# [1]" in res.source
    assert R._BLOCK_OPEN not in res.source
    side = res.files["REFERENCES.md"]
    assert "- [1] pa5 | Smith (2020)." in side
    assert "> the abstract text" in side
    assert R.strip_references(res.source, sidecar_text=side) == src


def test_excerpts_off_by_default() -> None:
    src = "x = 1  # pa5\n"
    res = R.expand_references(src, resolver=_resolver(), today=TODAY)
    assert "# [1] pa5 | Smith (2020)." in res.source
    assert "the abstract text" not in res.source


def test_gloss_handles_are_scrubbed() -> None:
    src = "x = 1  # gr5 (see gr5137)\n"
    res = R.expand_references(src, resolver=_resolver(), today=TODAY)
    assert "gr5137" not in res.source
    assert "# (internal ref, not exported) (see (internal ref, not exported))" in (
        res.source
    )


def test_gloss_scrub_leaves_non_handle_words() -> None:
    src = "x = 1  # pa5 (3V3 rail, xgr5137y)\n"
    res = R.expand_references(src, resolver=_resolver(), today=TODAY)
    assert "(3V3 rail, xgr5137y)" in res.source


def test_expand_bad_mode_refuses() -> None:
    with pytest.raises(BadInput):
        R.expand_references("x = 1\n", resolver=_resolver(), mode="bogus")


def test_orphaned_number_comment_kept() -> None:
    src = "x = 1  # [7] (why)\n"
    res = R.expand_references(src, resolver=_resolver(), today=TODAY)
    assert res.report.orphaned == 1
    assert res.source == src


# ── StoreRefResolver ────────────────────────────────────────────────────


class _FakeChunks:
    def __init__(self, rows: dict[int, list[Any]], pages: dict[int, int]) -> None:
        self._rows = rows
        self._pages = pages

    def get_chunk(self, ref_id: int, *, pos: Any = None, slug: Any = None) -> Any:
        for r in self._rows.get(ref_id, []):
            if pos is not None and r.ord == pos:
                return r
            if slug is not None and r.slug == slug:
                return r
        return None

    def list_chunks_for_ref(self, ref_id: int) -> list[Any]:
        return list(self._rows.get(ref_id, []))

    def chunk_pages(self, ref_id: int, ords: list[int]) -> dict[int, int]:
        return {o: self._pages[o] for o in ords if o in self._pages}


def _fake_store(
    resolved: Any, ref: Any, rows: list[Any], pages: dict[int, int], doi: str | None
) -> Any:
    rid = resolved.ref_id if resolved else -1
    store = SimpleNamespace()
    store.resolve_handle = lambda h, **kw: resolved
    store.get_ref = lambda **kw: ref
    store.identifiers_for_refs = lambda ids: (
        {rid: {"doi": doi}} if doi and rid >= 0 else {}
    )
    store.chunks = _FakeChunks({rid: rows}, pages)
    return store


def _ref(**kw: Any) -> Any:
    base = dict(
        kind="paper",
        slug="smith20",
        title="A paper",
        meta={},
        year=2020,
        authors=[{"family": "Smith"}, {"family": "Jones"}, {"family": "Doe"}],
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _chunk(ord_: int, text: str, slug: str | None = None) -> Any:
    return SimpleNamespace(ord=ord_, text=text, slug=slug)


def _resolved(**kw: Any) -> Any:
    base = dict(
        ref_id=1,
        kind="paper",
        public_id="smith20",
        chunk_id=None,
        chunk_ord=None,
        cite_public_id=None,
        redirected_from=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_resolver_paper_citation_and_excerpt() -> None:
    resolved = _resolved()
    store = _fake_store(resolved, _ref(), [_chunk(0, "the abstract")], {}, "10.1/x")
    cit = R.StoreRefResolver(store).cite("pa5")
    assert cit is not None
    assert cit.citation == 'Smith & Jones et al. (2020). "A paper". doi:10.1/x'
    assert cit.excerpt == "the abstract"


def test_resolver_chunk_handle_uses_its_chunk() -> None:
    resolved = _resolved(chunk_id=10, chunk_ord=3)
    rows = [_chunk(0, "first"), _chunk(3, "third chunk body")]
    store = _fake_store(resolved, _ref(), rows, {3: 12}, None)
    cit = R.StoreRefResolver(store).cite("pc10")
    assert cit is not None and cit.excerpt == "third chunk body"


def test_resolver_internal_kind_never_exported() -> None:
    store = SimpleNamespace(
        resolve_handle=lambda h, **kw: pytest.fail("no store hit"),
    )
    cit = R.StoreRefResolver(store).cite("gr5")
    assert cit is not None and cit.internal is True


def test_resolver_unresolved_returns_none() -> None:
    store = _fake_store(None, None, [], {}, None)
    assert R.StoreRefResolver(store).cite("pa999") is None
    assert R.StoreRefResolver(store).cite("zz9") is None


def test_resolver_structure_is_pointer_only() -> None:
    resolved = _resolved(kind="structure", public_id="mof-1")
    ref = _ref(
        kind="structure",
        slug="mof-1",
        title="",
        meta={"version": 3, "atom_count": 42},
        authors=None,
        year=None,
    )
    store = _fake_store(resolved, ref, [], {}, None)
    cit = R.StoreRefResolver(store).cite("st5")
    assert cit is not None
    assert cit.citation == "structure mof-1, v3, 42 atoms"
    assert cit.excerpt is None


def test_resolver_page_selector() -> None:
    resolved = _resolved()
    rows = [_chunk(0, "p1 text"), _chunk(1, "p5 text")]
    store = _fake_store(resolved, _ref(), rows, {0: 1, 1: 5}, None)
    cit = R.StoreRefResolver(store).cite("pa5~p3")
    assert cit is not None and cit.excerpt == "p1 text"
