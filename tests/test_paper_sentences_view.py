"""``view='sentences'`` — the numbered-sentence read (gr716229).

The measure evidence contract asks for a sentence-span anchor
(``anchor={'chunk': 'pc<id>', 'anchor_scheme': 'sentence', 'span': 's3'}``)
but the read side offered no way to see which text ``s3`` names — the
view was ``Unsupported``. This view renders a ``~N`` / ``~A..B`` chunk
selection (a ``pc<id>`` handle rewrites to ``<slug>~<ord>``) split into
sentence spans by the same splitter the anchor machinery uses, numbered
s0, s1, … so the span id is verifiable before the run is written.

DB-backed like ``tests/test_paper_tables_view.py``: real ``refs`` /
``chunks`` rows read back through ``PaperHandler.get`` and the runtime
dispatch path.
"""

from __future__ import annotations

import re

import pytest

from precis.dispatch import Hub
from precis.embedder import MockEmbedder
from precis.errors import BadInput
from precis.extract.anchors import sentence_spans
from precis.handlers.paper import PaperHandler
from precis.response import Response
from precis.runtime import PrecisRuntime
from precis.store import ChunkInsert, Store
from precis.utils import handle_registry

_PROSE = (
    "The faradaic efficiency reached 95 % under ambient conditions. "
    "A second sentence carries −0.5 V vs RHE. "
    "Table 2 summarizes the long-term stability tests."
)

_ABBREV = (
    "The rate constant was ca. 1.9 s−1 at 300 K. "
    "Fig. 3 shows the corresponding Arrhenius plot."
)


def _handler(store: Store) -> PaperHandler:
    return PaperHandler(hub=Hub(store=store, embedder=MockEmbedder(dim=1024)))


def _seed_paper(store: Store, *, slug: str) -> tuple[int, list[int]]:
    """Two prose chunks: the first the three-sentence _PROSE, the
    second an abbreviation-veto probe. Returns ``(ref_id, chunk_ids)``
    in ord order."""
    ref = store.insert_ref(kind="paper", slug=slug, title=slug)
    e = MockEmbedder(dim=1024)
    specs = [_PROSE, _ABBREV, ""]
    blocks = store.chunks.insert_chunks(
        ref.id,
        [
            ChunkInsert(ord=i, text=text, embedding=e.embed_one(text or slug))
            for i, text in enumerate(specs)
        ],
    )
    return ref.id, [b.id for b in blocks]


def test_sentences_view_numbers_chunk_spans_from_s0(store: Store) -> None:
    ref_id, chunk_ids = _seed_paper(store, slug="sview01")
    pa = handle_registry.format_handle("paper", ref_id)
    pc = handle_registry.format_handle("paper", chunk_ids[0], chunk=True)

    resp = _handler(store).get(id=f"{pa}~0", view="sentences")

    out = resp.body
    assert out.startswith(f"# {pa} — sentences ~0")
    assert f"## {pc} ~0" in out
    # One sN line per sentence_spans() span, 0-based, exact span text.
    spans = sentence_spans(_PROSE)
    assert resp.result_count == len(spans) == 3
    for n, (start, end) in enumerate(spans):
        assert f"s{n}: {_PROSE[start:end]}" in out
    # The anchor spelling the number names is printed next to the read.
    assert "'anchor_scheme': 'sentence', 'span': 'sN'" in out
    assert "precis-measure-help" in out
    # The ~ord address survives for a paste-back read of the raw chunk.
    assert f"get(id='{pc}')" in out


def test_sentences_view_uses_the_anchor_splitter(store: Store) -> None:
    """``ca.`` / ``Fig.`` periods must not split a span — the view's
    numbering is only verifiable if it is the anchor machinery's own
    splitter, not a fresh one."""
    ref_id, chunk_ids = _seed_paper(store, slug="sview02")
    pa = handle_registry.format_handle("paper", ref_id)
    pc = handle_registry.format_handle("paper", chunk_ids[1], chunk=True)

    resp = _handler(store).get(id=f"{pa}~1", view="sentences")

    spans = sentence_spans(_ABBREV)
    assert resp.result_count == len(spans) == 2
    assert "s0: The rate constant was ca. 1.9 s−1 at 300 K." in resp.body
    assert "s1: Fig. 3 shows the corresponding Arrhenius plot." in resp.body


def test_sentences_view_range_covers_each_chunk(store: Store) -> None:
    ref_id, chunk_ids = _seed_paper(store, slug="sview03")
    pa = handle_registry.format_handle("paper", ref_id)

    resp = _handler(store).get(id=f"{pa}~0..1", view="sentences")

    out = resp.body
    assert out.startswith(f"# {pa} — sentences ~0..1")
    pc0 = handle_registry.format_handle("paper", chunk_ids[0], chunk=True)
    pc1 = handle_registry.format_handle("paper", chunk_ids[1], chunk=True)
    assert f"## {pc0} ~0" in out
    assert f"## {pc1} ~1" in out
    # Numbering restarts per chunk — span ids are chunk-local.
    assert "s0: The faradaic efficiency reached 95 % under ambient conditions." in out
    assert "s0: The rate constant was ca. 1.9 s−1 at 300 K." in out
    assert resp.result_count == 5


def test_sentences_view_empty_chunk_names_no_spans(store: Store) -> None:
    ref_id, _chunk_ids = _seed_paper(store, slug="sview04")
    pa = handle_registry.format_handle("paper", ref_id)

    resp = _handler(store).get(id=f"{pa}~2", view="sentences")

    assert "(no sentence spans in this chunk)" in resp.body
    assert resp.result_count == 0


def test_sentences_view_ref_level_refuses_with_chunk_call(store: Store) -> None:
    ref_id, _chunk_ids = _seed_paper(store, slug="sview05")
    pa = handle_registry.format_handle("paper", ref_id)

    with pytest.raises(BadInput, match="chunk-scoped") as exc:
        _handler(store).get(id=pa, view="sentences")

    nxt = exc.value.next
    assert isinstance(nxt, str)
    assert f"get(id='{pa}~<ord>', view='sentences')" in nxt


def test_sentences_view_via_runtime_dispatch_pc_handle(
    runtime_with_store: PrecisRuntime,
) -> None:
    """The exact gr716229 shape through the MCP boundary: a ``pc<id>``
    chunk handle plus ``view='sentences'`` (the handle rewrites to
    ``<slug>~<ord>`` inside dispatch)."""
    store = runtime_with_store.store
    assert store is not None
    ref_id, chunk_ids = _seed_paper(store, slug="sview06")
    pc = handle_registry.format_handle("paper", chunk_ids[0], chunk=True)

    out, err = runtime_with_store.dispatch_with_status(
        "get", {"kind": "paper", "id": pc, "view": "sentences"}
    )

    assert not err, out
    assert f"## {pc} ~0" in out
    assert "s2: Table 2 summarizes the long-term stability tests." in out
    # The view is advertised on the kind's accepted-view list.
    bad, err2 = runtime_with_store.dispatch_with_status(
        "get", {"kind": "paper", "id": f"pa{ref_id}", "view": "sentenc"}
    )
    assert err2
    assert "sentences" in bad


def test_sentences_view_via_path_form(store: Store) -> None:
    """``pa<N>~<ord>/sentences`` — the path form precis-paper-help's
    kwarg/path equivalence promise covers; it must reach the same
    numbered render as ``view='sentences'``."""
    ref_id, _chunk_ids = _seed_paper(store, slug="sview07")
    pa = handle_registry.format_handle("paper", ref_id)

    resp = _handler(store).get(id=f"{pa}~1/sentences")

    assert resp.result_count == 2
    assert "sentences ~1" in resp.body
    assert "s1: Fig. 3 shows the corresponding Arrhenius plot." in resp.body


def test_sentences_view_lead_names_resolved_write_check(
    store: Store,
) -> None:
    """The lead must not promise a whole-chunk check: since gr728422
    ``_measures_ops._span_range`` resolves ``s<N>`` against this very
    numbering, so a wrong index reads ``anchor_mismatch``."""
    ref_id, _chunk_ids = _seed_paper(store, slug="sview08")
    pa = handle_registry.format_handle("paper", ref_id)

    resp = _handler(store).get(id=f"{pa}~0", view="sentences")

    assert "resolves sN" in resp.body and "whole chunk" not in resp.body


def test_sentences_view_cut_continuation_keeps_the_view(
    store: Store,
) -> None:
    """The fitted continuation must keep
    ``view='sentences'`` — a bare ``get(id='pa<N>~N..M')`` opens raw
    chunks and drops the numbering the caller was walking."""
    ref = store.insert_ref(kind="paper", slug="sview09", title="sview09")
    e = MockEmbedder(dim=1024)
    store.chunks.insert_chunks(
        ref.id,
        [
            ChunkInsert(ord=i, text=_PROSE, embedding=e.embed_one(_PROSE))
            for i in range(6)
        ],
    )
    pa = handle_registry.format_handle("paper", ref.id)
    resp = _handler(store).get(id=f"{pa}~0..5", view="sentences")

    assert resp.budget_fit is not None
    body = resp.budget_fit(330)
    assert "sentence page cut" in body
    assert f"rest: get(id='{pa}~1..5', view='sentences')" in body


def test_sentences_view_pc_handle_path_form_single_and_batch(
    runtime_with_store: PrecisRuntime,
) -> None:
    """``pc<id>/sentences`` kindless — review td732993: dispatch declined
    the chunk-handle view path before it reached the paper handler when
    ``kind`` was omitted, so both the single and the ``id=[...]`` batch
    read failed to route. Both must reach the numbered render through
    ``PrecisRuntime.dispatch_with_status`` with no ``kind=``."""
    store = runtime_with_store.store
    assert store is not None
    ref_id, chunk_ids = _seed_paper(store, slug="sview10")
    pc = handle_registry.format_handle("paper", chunk_ids[0], chunk=True)

    single, err = runtime_with_store.dispatch_with_status(
        "get", {"id": f"{pc}/sentences"}
    )
    assert not err, single
    assert f"## {pc} ~0" in single
    assert "s2: Table 2 summarizes the long-term stability tests." in single

    batch, err = runtime_with_store.dispatch_with_status(
        "get", {"id": [f"{pc}/sentences"]}
    )
    assert not err, batch
    assert "# 1 papers (batch)" in batch
    assert "s2: Table 2 summarizes the long-term stability tests." in batch
    assert isinstance(ref_id, int)


def test_sentences_view_window_form_keeps_chunk_local_numbering(
    store: Store,
) -> None:
    """``slug~0/sentences/s1..2`` — the resumable window selects spans
    by their chunk-local index and NEVER renumbers: the s<N> printed is
    still the id a measure anchor names."""
    ref_id, _chunk_ids = _seed_paper(store, slug="sview12")
    pa = handle_registry.format_handle("paper", ref_id)

    closed = _handler(store).get(id=f"{pa}~0/sentences/s1..2")
    assert closed.result_count == 2
    spans = sentence_spans(_PROSE)
    assert f"s1: {_PROSE[spans[1][0] : spans[1][1]]}" in closed.body
    assert f"s2: {_PROSE[spans[2][0] : spans[2][1]]}" in closed.body
    assert "s0: The faradaic efficiency" not in closed.body

    open_end = _handler(store).get(id=f"{pa}~0/sentences/s2..")
    assert open_end.result_count == 1
    assert f"s2: {_PROSE[spans[2][0] : spans[2][1]]}" in open_end.body


def test_sentences_view_window_beyond_chunk_is_empty(store: Store) -> None:
    ref_id, _chunk_ids = _seed_paper(store, slug="sview13")
    pa = handle_registry.format_handle("paper", ref_id)

    resp = _handler(store).get(id=f"{pa}~0/sentences/s40..80")

    assert resp.result_count == 0
    assert "(no sentence spans in this chunk)" in resp.body


@pytest.mark.parametrize("batch", [False, True])
def test_sentences_view_open_window_keeps_both_dots_through_dispatch(
    runtime_with_store: PrecisRuntime, batch: bool
) -> None:
    store = runtime_with_store.store
    assert store is not None
    _, chunk_ids = _seed_paper(store, slug="sviewopen")
    pc = handle_registry.format_handle("paper", chunk_ids[0], chunk=True)
    ident = f"{pc}/sentences/s1.."

    out, err = runtime_with_store.dispatch_with_status(
        "get", {"id": [ident] if batch else ident}
    )

    assert not err, out
    assert "s0: " not in out
    assert "s1: A second sentence" in out
    assert "s2: Table 2" in out


@pytest.mark.parametrize("oversized", [False, True])
@pytest.mark.parametrize("batch", [False, True])
def test_sentences_view_cut_followed_retrieves_omitted_numbered_lines(
    runtime_with_store: PrecisRuntime,
    monkeypatch: pytest.MonkeyPatch,
    oversized: bool,
    batch: bool,
) -> None:
    store = runtime_with_store.store
    assert store is not None
    big = "The sample contains a measurable quantity. " * 100
    if oversized:
        big = "An oversized " + "μ" * 3000 + " sentence. " + big
    ref = store.insert_ref(kind="paper", slug="sview11", title="sview11")
    store.chunks.insert_chunks(ref.id, [ChunkInsert(ord=0, text=big)])
    pa = handle_registry.format_handle("paper", ref.id)
    cap = 1300
    monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", "4000")
    monkeypatch.setenv("PRECIS_TOOL_RESULT_CONTEXT_BYTES", str(cap))
    # Batch shape uses the frame cap. In both shapes, runtime meters this
    # suffix before asking the handler for the next sentence boundary.
    if batch:
        monkeypatch.setenv("PRECIS_MAX_BODY_BYTES", str(cap))
    suffix = "\n[tip] " + "x" * 500

    def render(response: Response) -> str:
        return response.body + suffix

    monkeypatch.setattr(runtime_with_store, "_render", render)
    spans = sentence_spans(big)
    seen: list[int] = []
    ident = f"{pa}~0/sentences"
    for hop in range(20):
        out, err = runtime_with_store.dispatch_with_status(
            "get", {"id": [ident] if batch else ident}, fold=True
        )
        assert not err, out
        assert out.endswith(suffix)
        assert len(out.encode("utf-8")) <= cap
        rows = re.findall(r"^s(\d+): (.+)$", out, flags=re.M)
        assert rows, out
        for number, sentence in rows:
            index = int(number)
            start, end = spans[index]
            if oversized and index == 0:
                assert sentence.startswith("An oversized ")
                assert sentence.endswith("… [sentence excerpt]")
            else:
                assert sentence == big[start:end]
            seen.append(index)
        if oversized and hop == 0:
            assert seen == [0]
        next_call = re.search(r"rest: get\(id='([^']+/sentences/s(\d+)\.\.\d+)'\)", out)
        if next_call is None:
            break
        assert int(next_call[2]) == seen[-1] + 1
        ident = next_call[1]
    assert seen == list(range(len(spans)))
