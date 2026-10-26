"""``view='tables'`` + ``args={'chunk_kind': ...}`` — gr506762 slice 1.

DB-backed: real ``refs``/``chunks`` rows via ``insert_chunks`` with
``meta['chunk_kind']``, read back through ``PaperHandler.get`` and
through the runtime dispatch path (the ``__extras__`` channel that
carries ``args={...}`` in from the MCP boundary), mirroring
``tests/test_paper_summaries_view.py``.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.embedder import MockEmbedder
from precis.errors import BadInput
from precis.extract.proposals import VocabEntry
from precis.extract.recipes import expand_recipe
from precis.extract.tables import parse_table
from precis.handlers import _measure_render as render
from precis.handlers.paper import PaperHandler
from precis.runtime import PrecisRuntime
from precis.store import ChunkInsert, Store
from precis.store._measures_ops import MeasureAnchor, MeasureSpec
from precis.utils import handle_registry

_GRID_A = """| Source | sticking prefactor (ML) | rate (s <sup>-1</sup>) |
| --- | --- | --- |
| H2O | 3.2 | 13.5 \\pm 1.1^a |
| CO | 1.1 | 8.0 |
"""

_GRID_B = """| Gas | desorption energy (kJ/mol) |
| --- | --- |
| Ar | 12.4 |
"""

_BROKEN = "a collapsed HTML table run, no pipe grid here"


def _handler(store: Store) -> PaperHandler:
    return PaperHandler(hub=Hub(store=store, embedder=MockEmbedder(dim=1024)))


def _seed_paper(store: Store, *, slug: str) -> tuple[int, list[int]]:
    """A paper holding a caption chunk, two parseable table chunks and
    one ``chunk_kind='table'`` chunk that isn't a pipe grid. The caption
    is numbered ``Table 1``, so it can only ever attach to T1 — a
    ``match=`` word the caption carries hits T1 only.
    Returns ``(ref_id, chunk_ids)`` in ord order."""
    ref = store.insert_ref(kind="paper", slug=slug, title=slug)
    e = MockEmbedder(dim=1024)
    specs = [
        ("paragraph", "opening prose, no grid"),
        ("caption", "Table 1. sticking prefactors and rates."),
        ("table", _GRID_A),
        ("paragraph", "interlude prose, still no grid"),
        ("table", _GRID_B),
        # A non-table chunk must separate BROKEN from GRID_B —
        # ord-adjacent table chunks group into ONE logical table.
        ("paragraph", "tail prose, still no grid"),
        ("table", _BROKEN),
    ]
    blocks = store.chunks.insert_chunks(
        ref.id,
        [
            ChunkInsert(
                ord=i,
                text=text,
                meta={"chunk_kind": kind},
                embedding=e.embed_one(text),
            )
            for i, (kind, text) in enumerate(specs)
        ],
    )
    return ref.id, [b.id for b in blocks]


def _row_chunk_ids(body: str) -> set[int]:
    """The ``pc<id>`` heads of the hit rows (before the ``Next:``
    trailer), decoded to chunk ids."""
    table = body.split("Next:", 1)[0]
    ids = set()
    for ln in table.splitlines():
        if not ln.startswith("pc"):
            continue
        parsed = handle_registry.parse(ln.split("\t", 1)[0].strip())
        assert parsed is not None
        ids.add(parsed[2])
    return ids


def test_tables_view_renders_grid_caption_and_raw(store: Store) -> None:
    ref_id, chunk_ids = _seed_paper(store, slug="tblgrid07")
    pa = handle_registry.format_handle("paper", ref_id)

    out = _handler(store).get(id=pa, view="tables").body

    assert out.startswith(f"# {pa} — 3 tables")
    for n, cid in ((1, chunk_ids[2]), (2, chunk_ids[4]), (3, chunk_ids[6])):
        assert f"## T{n} · pc{cid}" in out
    # The caption chunk before T1 attaches with its own handle.
    assert f"caption (pc{chunk_ids[1]})" in out
    assert "sticking prefactors and rates" in out
    # Header-unit hint on the (ML) column; cell keeps its footnote.
    assert "[unit ML]" in out
    assert "13.5 ± 1.1^a" in out
    # The non-grid table chunk renders raw, never guessed at.
    assert "unparsed — raw text:" in out
    assert "collapsed HTML table run" in out
    # Parsed sections carry the measure counter and the cell-cite
    # shape; the unparsed one ends at its raw text.
    assert out.count("measures: 0 extracted") == 2
    assert f"cite a cell as pc{chunk_ids[2]} row R col C" in out


def test_tables_view_match_filter_keeps_word_hit(store: Store) -> None:
    ref_id, _chunk_ids = _seed_paper(store, slug="tblmatch07")
    pa = handle_registry.format_handle("paper", ref_id)

    out = _handler(store).get(id=pa, view="tables", args={"match": "sticking"}).body

    assert "(showing 1 table)" in out
    assert "## T1" in out
    assert "## T2" not in out
    assert "## T3" not in out

    dry = _handler(store).get(id=pa, view="tables", args={"match": "zebra"}).body
    assert "no tables in" in dry
    assert "zebra" in dry


def test_tables_view_table_select_ordinal_and_handle(store: Store) -> None:
    ref_id, chunk_ids = _seed_paper(store, slug="tblsel07")
    pa = handle_registry.format_handle("paper", ref_id)
    h = _handler(store)

    one = h.get(id=pa, view="tables", args={"table": 2}).body
    assert "## T2" in one
    assert "## T1" not in one

    by_handle = h.get(id=pa, view="tables", args={"table": f"pc{chunk_ids[4]}"}).body
    assert by_handle == one

    with pytest.raises(BadInput):
        h.get(id=pa, view="tables", args={"table": 9})
    with pytest.raises(BadInput):
        h.get(id=pa, view="tables", args={"table": f"pc{chunk_ids[0]}"})


def test_tables_view_no_tables_points_at_chunks_view(store: Store) -> None:
    ref = store.insert_ref(kind="paper", slug="tblnone07", title="tblnone07")
    store.chunks.insert_chunks(
        ref.id, [ChunkInsert(ord=0, text="all prose, no tables")]
    )
    pa = handle_registry.format_handle("paper", ref.id)

    out = _handler(store).get(id=pa, view="tables").body

    assert f"no tables in {pa}" in out
    assert "view='chunks'" in out


def test_get_tables_args_ride_the_dispatch_extras_channel(
    runtime_with_store: PrecisRuntime,
) -> None:
    """``args={'match': …}`` reaches ``get`` as an ``ArgsView`` gated on
    the declared ``{'match', 'table'}`` keys — an undeclared key refuses."""
    store = runtime_with_store.store
    assert store is not None
    ref_id, _c = _seed_paper(store, slug="tbldisp07")
    pa = handle_registry.format_handle("paper", ref_id)

    out = runtime_with_store.dispatch(
        "get",
        {
            "kind": "paper",
            "id": pa,
            "view": "tables",
            "__extras__": {"match": "sticking"},
        },
    )
    assert "## T1" in out
    assert "## T2" not in out

    bad = runtime_with_store.dispatch(
        "get",
        {
            "kind": "paper",
            "id": pa,
            "view": "tables",
            "__extras__": {"bogus": 1},
        },
    )
    assert "[error:BadInput]" in bad


def test_search_chunk_kind_restricts_to_table_hits(store: Store) -> None:
    """``args={'chunk_kind': 'table'}`` keeps only the table chunk even
    when a paragraph chunk carries the same word."""
    ref = store.insert_ref(kind="paper", slug="tblsrch07", title="tblsrch07")
    e = MockEmbedder(dim=1024)
    specs = [
        ("paragraph", "the sticking coefficient intro paragraph"),
        ("table", "| Gas | sticking coeff |\n| --- | --- |\n| H2 | 0.42 |\n"),
    ]
    blocks = store.chunks.insert_chunks(
        ref.id,
        [
            ChunkInsert(
                ord=i,
                text=text,
                meta={"chunk_kind": kind},
                embedding=e.embed_one(text),
            )
            for i, (kind, text) in enumerate(specs)
        ],
    )
    para_id, table_id = blocks[0].id, blocks[1].id
    h = _handler(store)

    both = h.search(q="sticking", unique_per="chunk")
    assert _row_chunk_ids(both.body) == {para_id, table_id}

    only_tables = h.search(q="sticking", chunk_kind="table")
    assert _row_chunk_ids(only_tables.body) == {table_id}


def test_search_chunk_kind_typo_refused_with_options(
    runtime_with_store: PrecisRuntime,
) -> None:
    """Through dispatch: the extras channel flattens ``chunk_kind`` to
    the search kwarg; an unknown slug lists the catalogue kinds."""
    out = runtime_with_store.dispatch(
        "search",
        {
            "kind": "paper",
            "q": "sticking",
            "__extras__": {"chunk_kind": "tabel"},
        },
    )
    assert "[error:BadInput]" in out
    assert "tabel" in out
    assert "table" in out
    # The options come from the chunk_kinds catalogue, not a hardcoded
    # body-kind list — a card kind only appears via the catalogue.
    assert "card_combined" in out


def test_search_chunk_kind_skips_title_introducer(store: Store) -> None:
    """``args={'chunk_kind': 'table'}`` must stay table-only even when q
    is a held paper's title — the title introducer would otherwise
    promote the paper's card above the filtered hits."""
    ref = store.insert_ref(
        kind="paper",
        slug="tbltitle07",
        title="sticking coefficients and desorption rates on nickel",
    )
    sha = "0" * 64
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO pdfs (pdf_sha256, content_hash, page_count, "
            "size_bytes, storage_path) VALUES (%s, %s, 1, 100, %s) "
            "ON CONFLICT (pdf_sha256) DO NOTHING",
            (sha, sha, "/tmp/x.pdf"),
        )
        conn.execute("UPDATE refs SET pdf_sha256 = %s WHERE ref_id = %s", (sha, ref.id))
    e = MockEmbedder(dim=1024)
    # The table text carries every content word of the title so the
    # lexical leg (adjacent terms AND) hits it under the kind filter.
    table_text = (
        "| Gas | sticking coefficients | desorption rates | nickel surface |\n"
        "| --- | --- | --- | --- |\n"
        "| H2 | 0.42 | 0.11 | Ni |\n"
    )
    blocks = store.chunks.insert_chunks(
        ref.id,
        [
            ChunkInsert(
                ord=0,
                text=table_text,
                meta={"chunk_kind": "table"},
                embedding=e.embed_one(table_text),
            )
        ],
    )
    table_id = blocks[0].id
    h = _handler(store)
    q = "sticking coefficients and desorption rates on nickel"

    plain = h.search(q=q, unique_per="chunk")
    assert "Title match" in plain.body

    filtered = h.search(q=q, chunk_kind="table")
    assert "Title match" not in filtered.body
    assert _row_chunk_ids(filtered.body) == {table_id}


def test_tables_view_split_chunks_share_t_and_caption(store: Store) -> None:
    """Ord-adjacent table chunks are one logical table (ingest's
    split_table): they share the ``T<n>`` number — continuations get a
    ``(part N)`` label — and one caption; ``args={'table': 1}`` renders
    the whole group."""
    ref = store.insert_ref(kind="paper", slug="tblsplit07", title="tblsplit07")
    e = MockEmbedder(dim=1024)
    # split_table repeats the header on every part — the same first
    # pipe line is what marks the continuation as part of Table 1.
    cont = (
        "| Source | sticking prefactor (ML) | rate (s <sup>-1</sup>) |\n"
        "| --- | --- | --- |\n"
        "| Xe | 5.5 | 1.0 |\n"
    )
    specs = [
        ("caption", "Table 1. Sticking coefficients."),
        ("table", _GRID_A),
        ("table", cont),  # ord-adjacent continuation of Table 1
        ("caption", "Table 2. Other rates."),
        ("table", _GRID_B),
    ]
    store.chunks.insert_chunks(
        ref.id,
        [
            ChunkInsert(
                ord=i,
                text=text,
                meta={"chunk_kind": kind},
                embedding=e.embed_one(text),
            )
            for i, (kind, text) in enumerate(specs)
        ],
    )
    pa = handle_registry.format_handle("paper", ref.id)
    h = _handler(store)

    out = h.get(id=pa, view="tables").body

    assert "## T1 · pc" in out
    assert "## T1 (part 2) · pc" in out
    assert "## T2 · pc" in out
    assert "## T3" not in out
    # Table 1's caption attaches to every part; Table 2's only to its own.
    assert out.count("Table 1. Sticking coefficients") == 2
    assert out.count("Table 2. Other rates") == 1

    one = h.get(id=pa, view="tables", args={"table": 1}).body
    assert "## T1 · pc" in one
    assert "## T1 (part 2)" in one
    assert "## T2" not in one


def test_tables_view_part_cap_folds_long_runs(store: Store) -> None:
    """A run of more than ``_PART_CAP`` (3) parts renders the first
    three plus a fold line in enumeration; ``args={'table': n}``
    renders the whole run."""
    ref = store.insert_ref(kind="paper", slug="tblparts07", title="tblparts07")
    e = MockEmbedder(dim=1024)
    header = "| Gas | coeff |"
    specs = [("caption", "Table 1. Many rows.")] + [
        ("table", f"{header}\n| --- | --- |\n| X{i} | {i}.5 |\n") for i in range(5)
    ]
    store.chunks.insert_chunks(
        ref.id,
        [
            ChunkInsert(
                ord=i,
                text=text,
                meta={"chunk_kind": kind},
                embedding=e.embed_one(text),
            )
            for i, (kind, text) in enumerate(specs)
        ],
    )
    pa = handle_registry.format_handle("paper", ref.id)
    h = _handler(store)

    out = h.get(id=pa, view="tables").body
    assert out.startswith(f"# {pa} — 1 table")
    assert "## T1 · pc" in out
    assert "## T1 (part 2) · pc" in out
    assert "## T1 (part 3) · pc" in out
    assert "(part 4)" not in out
    assert "… 2 more parts of T1 — args={'table': 1}" in out
    # The caption renders only on the shown parts.
    assert out.count("Table 1. Many rows.") == 3

    one = h.get(id=pa, view="tables", args={"table": 1}).body
    for part in range(2, 6):
        assert f"## T1 (part {part})" in one
    assert one.count("Table 1. Many rows.") == 5


def test_tables_view_budget_counts_groups_not_sections(store: Store) -> None:
    """``_TABLE_BUDGET`` counts shown *table groups*: 4 tables × 5
    parts render 3 parts + a fold line each (4 sections per group),
    so a ``len(sections)`` budget would fold the 4th table early.
    ``result_count`` counts tables, not rendered sections."""
    ref = store.insert_ref(kind="paper", slug="tblbudget07", title="tblbudget07")
    e = MockEmbedder(dim=1024)
    # Distinct first pipe lines keep the five-part runs separate even
    # though every table chunk is ord-adjacent to the next.
    specs = [
        ("table", f"| Gas{t} | coeff |\n| --- | --- |\n| X{t}{i} | {i}.5 |\n")
        for t in range(4)
        for i in range(5)
    ]
    store.chunks.insert_chunks(
        ref.id,
        [
            ChunkInsert(
                ord=i,
                text=text,
                meta={"chunk_kind": kind},
                embedding=e.embed_one(text),
            )
            for i, (kind, text) in enumerate(specs)
        ],
    )
    pa = handle_registry.format_handle("paper", ref.id)
    h = _handler(store)

    resp = h.get(id=pa, view="tables")
    out = resp.body
    assert out.startswith(f"# {pa} — 4 tables")
    for n in range(1, 5):
        assert f"## T{n} · pc" in out
        assert f"## T{n} (part 3) · pc" in out
        assert f"… 2 more parts of T{n} — args={{'table': {n}}}" in out
    # No group-level fold pointers ("… T<n> pc…") — every table rendered.
    assert not [ln for ln in out.splitlines() if ln.startswith("… T")]
    assert resp.result_count == 4

    # Selecting a table renders all five parts but still counts one table.
    one = h.get(id=pa, view="tables", args={"table": 2})
    assert "## T2 (part 5)" in one.body
    assert one.result_count == 1


def test_tables_view_hard_max_tail_uses_the_run_header(store: Store) -> None:
    """gr506762 r3 A: an ``enforce_hard_max`` continuation opens on a
    data row — the view parses it against the run head's header, so
    the rendered grid numbers the same rows ``meta.table`` does:
    ``pc<tail> row 1 col 2`` is ``60``, not ``30`` under a 'Pd mem'
    pseudo-header."""
    ref = store.insert_ref(kind="paper", slug="tblcont07", title="tblcont07")
    e = MockEmbedder(dim=1024)
    head = (
        "| Catalyst | FE NH3 (%) | j_total (A m-2) |\n"
        "| --- | --- | --- |\n"
        "| Cu NWA | 95 | 1000 |\n"
    )
    tail = "| Pd mem | 60 | 800 |\n| Au film | 30 | 400 |\n"
    blocks = store.chunks.insert_chunks(
        ref.id,
        [
            ChunkInsert(
                ord=i,
                text=text,
                meta={"chunk_kind": "table"},
                embedding=e.embed_one(text),
            )
            for i, text in enumerate((head, tail))
        ],
    )
    pa = handle_registry.format_handle("paper", ref.id)

    out = _handler(store).get(id=pa, view="tables").body

    assert f"## T1 (part 2) · pc{blocks[1].id}" in out
    part2 = out.split("## T1 (part 2)", 1)[1]
    # The head's column labels, not the tail's first data row.
    assert "FE NH3 (%)" in part2 and "jtotal (A m-2)" in part2
    assert "Pd mem" not in part2.split("columns:", 1)[1].split("\n", 1)[0]
    # Row 1 is the tail's first line — meta.table row 1 col 2 == 60.
    assert "| 1 | Pd mem | 60 | 800 |" in part2
    assert "| 2 | Au film | 30 | 400 |" in part2


def test_tables_args_refused_off_the_tables_view(store: Store) -> None:
    """``args={'table'|'match'}`` on another view (or no view) refuses
    naming ``view='tables'`` instead of being silently dropped."""
    ref_id, _chunk_ids = _seed_paper(store, slug="tblargs07")
    pa = handle_registry.format_handle("paper", ref_id)
    h = _handler(store)

    with pytest.raises(BadInput, match="view='tables'"):
        h.get(id=pa, view="toc", args={"match": "sticking"})
    with pytest.raises(BadInput, match="view='tables'"):
        h.get(id=pa, args={"table": 1})
    # The tables view itself still accepts them.
    assert "## T1" in h.get(id=pa, view="tables", args={"table": 1}).body


@pytest.fixture
def paper_measures(store: Store) -> tuple[int, list[int], dict[str, int]]:
    """Mixed inventory: live and superseded outputs, an input, a flagged
    table value, promoted subject, and an unanchored prose value."""
    paper, chunks = _seed_paper(store, slug="tblmeasures03")
    taxon = store.insert_ref(
        kind="taxon",
        slug=None,
        title="Table coefficient",
        meta={
            "name": "Table coefficient",
            "norm_name": "table coefficient",
            "slug": "table-coefficient",
            "definition": "A coefficient.",
            "aliases": [],
            "status": "proposed",
            "canonical_unit": "1",
        },
    )

    def spec(
        literal: str,
        *,
        table: int | None = None,
        subject: int = paper,
        ambiguous: bool = False,
        supersedes: int | None = None,
    ) -> MeasureSpec:
        chunk = chunks[2] if table == 1 else chunks[4]
        return MeasureSpec(
            measurand_ref_id=taxon.id,
            subject_ref_id=subject,
            literal=literal,
            reported_unit="1",
            subject="Table material",
            anchor=MeasureAnchor(paper, chunk, "sentence", "s1") if table else None,
            meta={
                "table": {
                    "chunk": chunk,
                    "table": table,
                    "row": 1,
                    "col": 2,
                    "header": "coefficient",
                }
            }
            if table
            else {},
            measurand_status="ambiguous" if ambiguous else None,
            supersedes=supersedes,
        )

    ids: dict[str, int] = {}
    first = store.insert_measure(
        spec("3.2", table=1), [spec("1.1", table=1)], actor="reader"
    )
    ids["t1"] = first.output_id
    ids["input"] = first.input_ids[0]
    old = store.insert_measure(spec("1.1", table=1), [], actor="reader")
    ids["old"] = old.output_id
    ids["replacement"] = store.insert_measure(
        spec("8.0", table=1, supersedes=old.output_id), [], actor="reader"
    ).output_id
    material = store.insert_ref(
        kind="material", slug="promoted-table-material", title="Promoted material"
    )
    ids["t2"] = store.insert_measure(
        spec("12.4", table=2, subject=material.id), [], actor="reader"
    ).output_id
    ids["flagged"] = store.insert_measure(
        spec("12.4", table=2, ambiguous=True), [], actor="reader"
    ).output_id
    ids["promoted"] = store.insert_measure(
        spec("1.1", table=1, subject=material.id), [], actor="reader"
    ).output_id
    ids["prose"] = store.insert_measure(spec("1"), [], actor="reader").output_id
    return paper, chunks, ids


def test_measures_view_groups_live_outputs_then_prose(
    store: Store, paper_measures: tuple[int, list[int], dict[str, int]]
) -> None:
    paper, _chunks, ids = paper_measures
    response = _handler(store).get(id=f"pa{paper}", view="measures")
    out = response.body
    assert response.result_count == 5
    assert out.startswith(f"# pa{paper} — 5 measure(s)")
    assert out.index("## T1") < out.index("## T2") < out.index("## prose")
    sections = out.split("## T1\n", 1)[1].split("## T2\n", 1)
    for key in ("t1", "replacement", "promoted"):
        assert f"mx{ids[key]} " in sections[0]
    assert f"mx{ids['t2']} " in sections[1].split("## prose", 1)[0]
    assert f"mx{ids['flagged']} " not in out
    assert f"mx{ids['prose']} " in out.split("## prose", 1)[1]
    assert f"mx{ids['input']} " not in out and f"mx{ids['old']} " not in out
    lines = [line for line in out.splitlines() if line.startswith("mx")]
    rows = store.measures_for_paper(paper)
    assert sorted(lines) == sorted(render.result_line(row) for row in rows)
    assert "ambiguous measurand" not in out


def test_tables_counts_unsuperseded_outputs_including_flagged_and_promoted(
    store: Store, paper_measures: tuple[int, list[int], dict[str, int]]
) -> None:
    paper, _chunks, _ids = paper_measures
    out = _handler(store).get(id=f"pa{paper}", view="tables").body
    t1, remainder = out.split("## T1", 1)[1].split("## T2", 1)
    t2 = remainder.split("## T3", 1)[0]
    assert "measures: 3 extracted" in t1
    assert "measures: 2 extracted" in t2


def test_measures_view_empty_and_runtime_dispatch(
    runtime_with_store: PrecisRuntime,
) -> None:
    store = runtime_with_store.store
    assert store is not None
    paper, _chunks = _seed_paper(store, slug="tblmeasureempty03")
    response = _handler(store).get(id=f"pa{paper}", view="measures")
    assert response.body == f"no measures in pa{paper}"
    assert response.result_count == 0
    out = runtime_with_store.dispatch(
        "get", {"kind": "paper", "id": f"pa{paper}", "view": "measures"}
    )
    assert f"no measures in pa{paper}" in out
    assert "[error:" not in out


def test_recipe_outputs_reach_paper_views_and_runtime(
    runtime_with_store: PrecisRuntime,
) -> None:
    store = runtime_with_store.store
    assert store is not None
    paper = store.insert_ref(kind="paper", slug="tblrecipe03", title="Recipe paper")
    text = "| Site | probability |\n| --- | --- |\n| Ni | 0.42 |\n"
    (chunk,) = store.chunks.insert_chunks(
        paper.id, [ChunkInsert(ord=0, text=text, meta={"chunk_kind": "table"})]
    )
    taxon = store.insert_ref(
        kind="taxon",
        slug=None,
        title="Probability",
        meta={
            "name": "Probability",
            "norm_name": "probability",
            "slug": "probability",
            "definition": "A probability.",
            "aliases": [],
            "status": "proposed",
            "canonical_unit": "1",
        },
    )
    parsed = parse_table(chunk.id, text)
    assert parsed is not None
    runs, drops = expand_recipe(
        parsed,
        {
            "table": {
                "orientation": "columns",
                "subject_axis": 0,
                "attribution": "own_work",
                "tier": "measured",
                "series": [
                    {
                        "index": 1,
                        "measurand": "probability",
                        "unit": "1",
                        "role": "output",
                    }
                ],
            }
        },
        chunk_id=chunk.id,
        caption=None,
        paper=paper.id,
        table=1,
        chunk_text=text,
        vocab={
            taxon.id: VocabEntry(
                ref_id=taxon.id,
                slug="probability",
                name="Probability",
                aliases=(),
                canonical_unit="1",
                display_unit="1",
                required_conditions=(),
            )
        },
    )
    assert drops == [] and len(runs) == 1
    (run,) = runs
    written = store.insert_measure(run.output, run.inputs, actor="reader")
    out = runtime_with_store.dispatch(
        "get", {"kind": "paper", "id": f"pa{paper.id}", "view": "measures"}
    )
    assert "## T1" in out and f"mx{written.output_id} " in out and "0.42" in out
    assert "[error:" not in out
    assert (
        "measures: 1 extracted" in _handler(store).get(id=paper.id, view="tables").body
    )
