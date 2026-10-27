"""DatasheetHandler — the evidence-role sibling of PaperHandler.

Verifies the *declared* differences from paper (corpus_role, no put,
restricted views, handle code) plus the datasheet-of relation wiring, without
ingesting a real PDF. The shared get/search machinery is exercised by the
paper handler tests; here we only assert the datasheet-specific spec + a
registration smoke test."""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.datasheet import DatasheetHandler
from precis.handlers.paper import PaperHandler
from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.store.types import _INVERSE_RELATIONS
from precis.utils import handle_registry
from tests.workers._helpers import seed_chunk


def test_datasheet_is_an_evidence_role_document() -> None:
    assert DatasheetHandler.spec.kind == "datasheet"
    # Unlike a cfp (spec), a datasheet *is* citable — but in its own kind so
    # it never mixes into academic paper search.
    assert DatasheetHandler.spec.corpus_role == "evidence"


def test_datasheet_subclasses_paper_for_dry_reuse() -> None:
    assert issubclass(DatasheetHandler, PaperHandler)


def test_datasheet_put_is_a_url_pull_and_has_no_citation_export_views(
    hub: Hub,
) -> None:
    # put queues a URL pull (gr492689); it never mints a stub.
    assert DatasheetHandler.spec.supports_put is True
    # Reading verbs are inherited.
    assert DatasheetHandler.spec.supports_get is True
    assert DatasheetHandler.spec.supports_search is True
    # Citation-export views are dropped (evidence for a part, not a bib entry).
    views = DatasheetHandler(hub=hub).accepted_views()
    assert "bibtex" not in views
    assert "ris" not in views
    assert "bibliography" not in views
    # …but the reading views carry over.
    assert "toc" in views
    assert "abstract" in views
    # gr520326: the figure-crop / page-render door — datasheet is the
    # kind that motivated it, so it must accept the view.
    assert "page_image" in views


def test_datasheet_handle_codes_registered() -> None:
    assert handle_registry.code_for_kind("datasheet") == "da"
    assert handle_registry.code_for_kind("datasheet", chunk=True) == "dk"
    kind, is_chunk = handle_registry.kind_for_code("da")
    assert kind == "datasheet" and is_chunk is False


def test_datasheet_of_relation_is_registered_with_inverse() -> None:
    # Migration 0054 seeds the part-linkage relation the handler docstring
    # advertises; the Literal + inverse map must stay in sync with the seed.
    assert _INVERSE_RELATIONS["datasheet-of"] == "has-datasheet"
    assert _INVERSE_RELATIONS["has-datasheet"] == "datasheet-of"


def test_meta_patch_normalises_vendor_subtype_part() -> None:
    patch = DatasheetHandler._datasheet_meta_patch(
        "  Espressif Systems  ", "app-note", " c2934569 "
    )
    assert patch == {
        "vendor": "Espressif Systems",
        "subtype": "app-note",
        "part_lcsc": "C2934569",  # upper-cased LCSC C-number
    }


def test_meta_patch_omitted_fields_untouched() -> None:
    # None ⇒ leave alone; blank string ⇒ explicit clear.
    assert DatasheetHandler._datasheet_meta_patch(None, None, None) == {}
    assert DatasheetHandler._datasheet_meta_patch("", None, None) == {"vendor": ""}


def test_meta_patch_rejects_unknown_subtype() -> None:
    with pytest.raises(BadInput):
        DatasheetHandler._datasheet_meta_patch(None, "brochure", None)


def test_paper_edit_fields_cover_paper_bib_signature() -> None:
    """Regression (gr202712): every bibliographic kwarg PaperHandler.edit
    accepts must be forwarded by the datasheet allow-list — journal/entry_type
    were silently dropped after cfc88e89 widened the paper editor."""
    import inspect

    from precis.handlers.datasheet import _PAPER_EDIT_FIELDS

    paper_params = inspect.signature(PaperHandler.edit).parameters
    assert {"journal", "entry_type"} <= set(_PAPER_EDIT_FIELDS)
    for field in _PAPER_EDIT_FIELDS:
        assert field in paper_params, f"{field} not a PaperHandler.edit kwarg"


def test_edit_with_no_fields_is_rejected(store: Store) -> None:
    handler = DatasheetHandler(hub=Hub(store=store))
    with pytest.raises(BadInput):
        handler.edit(id="whatever")  # no meta + no bibliographic field


def test_datasheet_empty_search_names_datasheet_not_paper(store: Store) -> None:
    """Regression: datasheet subclasses PaperHandler and reuses its search
    path, whose empty-result branch once hardcoded the noun as "no paper
    blocks match" — leaking the *paper* kind on a datasheet miss."""
    lex_only = DatasheetHandler(hub=Hub(store=store))
    resp = lex_only.search(q="zzqqxx-no-such-token")
    assert "no datasheet blocks match" in resp.body
    assert "paper" not in resp.body


def test_datasheet_empty_list_names_datasheet_not_paper(store: Store) -> None:
    """Regression (gr311347 #12): the ``get(id=None)`` empty-corpus
    listing inherited PaperHandler's "no papers ingested yet - use
    `precis jobs ingest-bundles`" — paper-pipeline phrasing that doesn't
    apply to a datasheet (acquired via a part's ``datasheet_url`` or the
    ``<inbox>/datasheets/`` watch dir, never that job)."""
    resp = DatasheetHandler(hub=Hub(store=store)).get()
    assert "no datasheets ingested yet" in resp.body
    assert "ingest-bundles" not in resp.body
    assert "datasheets/" in resp.body


def test_datasheet_put_ssrf_refusal_carries_next(store: Store) -> None:
    """gr494831 M3: a hint-less raise is a bug; the SSRF refusal must name
    a copy-pasteable recovery call."""
    handler = DatasheetHandler(hub=Hub(store=store))
    with pytest.raises(BadInput, match="url refused") as ei:
        handler.put(args={"url": "http://127.0.0.1/x.pdf", "part": "C19191684"})
    assert ei.value.next and "put(kind='datasheet'" in str(ei.value.next)


# ── put(args={'stub': True}) — the manual-fill door (gr520099) ──────


def _catalog_row(n: int) -> dict[str, object]:
    from precis.pcb import catalog

    row = catalog.normalize_jlcparts_row(
        {
            "lcsc": n,
            "manufacturer": "Acme",
            "mfr_part": f"ACME{n}",
            "description": "10k 0402 resistor",
            "basic": 1,
            "stock": 100,
            "package": "0402",
            "datasheet": None,
        }
    )
    assert row is not None
    return row


def test_put_stub_mints_linked_bodiless_datasheet(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """``args={'part': 'C…', 'stub': True}`` mints a bodiless datasheet
    titled ``<mfr> <mfr_part>``, slugged ``<mfr>_<MPN>`` (the gr519904
    ruling), already linked ``datasheet-of`` the part's lazy ref — no
    pull, no facts job."""
    from precis.pcb.datasheets import datasheet_state
    from precis.utils import handle_registry

    store.parts_import([_catalog_row(25900)])
    out = runtime_with_store.dispatch(
        "put", {"kind": "datasheet", "args": {"part": "C25900", "stub": True}}
    )
    assert "datasheet stub da" in out
    assert "<inbox>/datasheets/" in out

    rows = store.stub_backlog(kind="datasheet")
    assert len(rows) == 1
    row = rows[0]
    assert row["title"] == "Acme ACME25900"
    assert row["part_lcsc"] == "C25900"
    handle = handle_registry.format_handle("datasheet", row["ref_id"])
    assert handle in out

    ref = store.get_ref(kind="datasheet", id=row["ref_id"])
    assert ref is not None and ref.slug == "acme_ACME25900"
    assert (ref.meta or {})["subtype"] == "datasheet"
    assert (ref.meta or {})["vendor"] == "Acme"

    ds = datasheet_state(store, "C25900")["datasheet"]
    assert ds == (ref.id, "acme_ACME25900")
    # A stub queues no pull: the part carries no datasheet_pull record.
    assert datasheet_state(store, "C25900")["pull"] is None


def test_put_stub_is_idempotent(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    store.parts_import([_catalog_row(25900)])
    runtime_with_store.dispatch(
        "put", {"kind": "datasheet", "args": {"part": "C25900", "stub": True}}
    )
    again = runtime_with_store.dispatch(
        "put", {"kind": "datasheet", "args": {"part": "C25900", "stub": True}}
    )
    assert "already tracks" in again
    assert len(store.stub_backlog(kind="datasheet")) == 1


def test_put_stub_refuses_the_url_combo(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    out = runtime_with_store.dispatch(
        "put",
        {
            "kind": "datasheet",
            "args": {"part": "C25900", "stub": True, "url": "https://x.test/a.pdf"},
        },
    )
    assert "stub and url" in out
    assert store.stub_backlog(kind="datasheet") == []


def test_put_stub_unknown_part_is_refused(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    # _resolve_part gates the stub door the same as the pull door: a
    # C-number the catalog doesn't hold (and JLC can't resolve) is a
    # NotFound, not a mint.
    out = runtime_with_store.dispatch(
        "put", {"kind": "datasheet", "args": {"part": "C99999999", "stub": True}}
    )
    assert "not in the catalog" in out
    assert store.stub_backlog(kind="datasheet") == []


# ── search views: status / ingest / source line (gr520077) ──────────
#
# ``view='status'`` answers "did this pull work, and what body did it
# leave" without SQL — the pull record lives on the part's
# ``meta.datasheet_pull``, body state on the linked datasheet.
# ``view='ingest'`` lists recent landings per ref (not paper's per-day
# rate). ``view='chase-queue'`` stays paper-only.


def _ds(
    store: Store,
    *,
    cite_key: str,
    title: str = "X",
    part_lcsc: str = "",
    source_url: str = "",
    subtype: str = "",
) -> int:
    meta: dict[str, object] = {}
    if part_lcsc:
        meta["part_lcsc"] = part_lcsc
    if source_url:
        meta["source_url"] = source_url
    if subtype:
        meta["subtype"] = subtype
    return store.insert_ref(kind="datasheet", slug=cite_key, title=title, meta=meta).id


def test_status_view_datasheet_row_shows_part_pull(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    rid = _ds(store, cite_key="kxob25", title="KXOB25-05X3F", part_lcsc="C5358484")
    pid = store.ensure_part_ref("C5358484", uncatalogued_ok=True)
    store.update_ref(
        pid,
        meta_patch={
            "datasheet_pull": {
                "status": "failed",
                "reason": "not_pdf",
                "at": "2026-10-25T00:00:00Z",
                "url": "https://example.com/x.html",
            }
        },
    )
    handle = handle_registry.format_handle("datasheet", rid)
    out = runtime_with_store.dispatch(
        "search", {"kind": "datasheet", "view": "status", "ids": [handle]}
    )
    assert handle in out
    assert "[kxob25]" in out
    assert "stub" in out
    assert "C5358484" in out
    assert "pull: failed: not_pdf" in out
    assert "https://example.com/x.html" in out


def test_status_view_c_number_shows_pull_and_linked(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    rid = _ds(store, cite_key="refds", title="ref manual", part_lcsc="C25804")
    seed_chunk(store, ref_id=rid, ord=0, text="pinout table")
    pid = store.ensure_part_ref("C25804", uncatalogued_ok=True)
    store.add_link(src_ref_id=rid, dst_ref_id=pid, relation="datasheet-of")
    store.update_ref(
        pid,
        meta_patch={
            "datasheet_pull": {
                "status": "ok",
                "url": "https://example.com/ds.pdf",
                "at": "2026-10-24T00:00:00Z",
                "last_attempt": {
                    "status": "failed",
                    "reason": "fetch_timeout",
                    "at": "2026-10-25T00:00:00Z",
                },
            }
        },
    )
    out = runtime_with_store.dispatch(
        "search", {"kind": "datasheet", "view": "status", "ids": ["C25804"]}
    )
    assert "C25804" in out
    assert "pull: ok" in out
    assert "latest re-pull failed: fetch_timeout" in out
    handle = handle_registry.format_handle("datasheet", rid)
    assert handle in out
    assert "[refds]" in out
    assert "full-text" in out


def test_status_view_part_never_pulled(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    store.ensure_part_ref("C25900", uncatalogued_ok=True)
    out = runtime_with_store.dispatch(
        "search", {"kind": "datasheet", "view": "status", "ids": ["C25900"]}
    )
    assert "pull: never tried" in out
    assert "(no datasheet linked)" in out


def test_status_view_needs_ids(runtime_with_store: PrecisRuntime) -> None:
    out = runtime_with_store.dispatch("search", {"kind": "datasheet", "view": "status"})
    assert "ids=" in out


def test_ingest_view_lists_recent_datasheets(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    rid = _ds(
        store,
        cite_key="newds",
        title="New Sheet",
        part_lcsc="C5156220",
        source_url="https://example.com/n.pdf",
    )
    seed_chunk(store, ref_id=rid, ord=0, text="body text")
    stub = _ds(store, cite_key="stubds", title="stub sheet")
    # A same-age paper must not leak into the datasheet listing.
    store.insert_ref(kind="paper", slug="pp2024x", title="paper x")
    out = runtime_with_store.dispatch("search", {"kind": "datasheet", "view": "ingest"})
    assert "datasheets landed" in out
    assert handle_registry.format_handle("datasheet", rid) in out
    assert "[newds]" in out
    assert "New Sheet" in out
    assert "part C5156220" in out
    assert "source https://example.com/n.pdf" in out
    assert "1 chunks" in out
    assert "[stubds]" in out
    assert handle_registry.format_handle("datasheet", stub) in out
    assert "pp2024x" not in out


def test_ingest_view_days_validated(runtime_with_store: PrecisRuntime) -> None:
    for bad in (0, 91):
        out = runtime_with_store.dispatch(
            "search", {"kind": "datasheet", "view": "ingest", "days": bad}
        )
        assert "days" in out, out


def test_overview_shows_source_url(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    rid = _ds(
        store, cite_key="srcds", title="t", source_url="https://example.com/ds.pdf"
    )
    out = runtime_with_store.dispatch(
        "get",
        {"kind": "datasheet", "id": handle_registry.format_handle("datasheet", rid)},
    )
    assert "source: https://example.com/ds.pdf" in out


def test_unknown_search_view_omits_chase_queue(store: Store) -> None:
    hub = Hub(store=store)
    with pytest.raises(BadInput) as ei:
        DatasheetHandler(hub=hub).search(view="bogus")
    options = ei.value.options or []
    assert "chase-queue" not in options
    assert "status" in options and "ingest" in options
    with pytest.raises(BadInput) as ei2:
        PaperHandler(hub=hub).search(view="bogus")
    assert "chase-queue" in (ei2.value.options or [])


def test_chase_queue_refuses_datasheet(runtime_with_store: PrecisRuntime) -> None:
    out = runtime_with_store.dispatch(
        "search", {"kind": "datasheet", "view": "chase-queue"}
    )
    assert "paper-only" in out
    # kind='paper' is still served.
    ok = runtime_with_store.dispatch("search", {"kind": "paper", "view": "chase-queue"})
    assert "paper-only" not in ok


# ── edit bib fields + part-card primary datasheet (gr520510) ────────
#
# DatasheetHandler.edit forwards ``**bib`` — a computed dict — so the
# dispatcher's MRO walk stops at its own signature: a bibliographic
# kwarg that isn't named there is refused both top-level and in
# ``args=``. The bib fields are now named params. The part card's
# primary datasheet prefers ``subtype`` 'datasheet'/'reference-manual'
# over 'app-note', newest ``datasheet-of`` link breaking ties.


def test_edit_accepts_title_top_level(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    rid = _ds(store, cite_key="titleds", title="Old Title")
    handle = handle_registry.format_handle("datasheet", rid)
    out = runtime_with_store.dispatch(
        "edit", {"kind": "datasheet", "id": handle, "title": "New Title"}
    )
    assert "error" not in out.lower()
    ref = store.get_ref(kind="datasheet", id=rid)
    assert ref is not None and ref.title == "New Title"


def test_edit_accepts_title_in_args(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    rid = _ds(store, cite_key="argds", title="Old Title")
    out = runtime_with_store.dispatch(
        "edit",
        {
            "kind": "datasheet",
            "id": handle_registry.format_handle("datasheet", rid),
            "__extras__": {"title": "Args Title"},
        },
    )
    assert "error" not in out.lower()
    ref = store.get_ref(kind="datasheet", id=rid)
    assert ref is not None and ref.title == "Args Title"


def test_edit_accepts_reason(runtime_with_store: PrecisRuntime, store: Store) -> None:
    rid = _ds(store, cite_key="reasonds", title="Old Title")
    out = runtime_with_store.dispatch(
        "edit",
        {
            "kind": "datasheet",
            "id": handle_registry.format_handle("datasheet", rid),
            "title": "Reasoned",
            "reason": "title was the MPN, not the doc name",
        },
    )
    assert "error" not in out.lower()
    ref = store.get_ref(kind="datasheet", id=rid)
    assert ref is not None and ref.title == "Reasoned"


def test_edit_still_refuses_an_unknown_kwarg(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    rid = _ds(store, cite_key="bogusds", title="Old Title")
    out = runtime_with_store.dispatch(
        "edit",
        {
            "kind": "datasheet",
            "id": handle_registry.format_handle("datasheet", rid),
            "bogus_field": "x",
        },
    )
    assert "bogus_field" in out


def test_datasheet_states_prefers_canonical_subtype(store: Store) -> None:
    """The app-note linked newest (and minted first — the prod shape that
    surfaced this) must not beat a reference-manual for the primary slot."""
    pid = store.ensure_part_ref("C2895249", uncatalogued_ok=True)
    appnote = _ds(store, cite_key="appnote", title="app note", subtype="app-note")
    manual = _ds(
        store, cite_key="refman", title="ref manual", subtype="reference-manual"
    )
    store.add_link(src_ref_id=appnote, dst_ref_id=pid, relation="datasheet-of")
    store.add_link(src_ref_id=manual, dst_ref_id=pid, relation="datasheet-of")
    # Give the app-note the *newer* link — subtype still outranks recency.
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE links SET created_at = '2026-06-01' WHERE src_ref_id = %s",
            (appnote,),
        )
        conn.execute(
            "UPDATE links SET created_at = '2026-01-01' WHERE src_ref_id = %s",
            (manual,),
        )
        conn.commit()
    from precis.pcb.datasheets import datasheet_state

    assert datasheet_state(store, "C2895249")["datasheet"] == (manual, "refman")


def test_datasheet_states_tie_goes_to_newest_link(store: Store) -> None:
    pid = store.ensure_part_ref("C25804", uncatalogued_ok=True)
    older = _ds(store, cite_key="olderds", title="older")
    newer = _ds(store, cite_key="newerds", title="newer")
    store.add_link(src_ref_id=older, dst_ref_id=pid, relation="datasheet-of")
    store.add_link(src_ref_id=newer, dst_ref_id=pid, relation="datasheet-of")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE links SET created_at = '2026-06-01' WHERE src_ref_id = %s",
            (older,),
        )
        conn.execute(
            "UPDATE links SET created_at = '2026-01-01' WHERE src_ref_id = %s",
            (newer,),
        )
        conn.commit()
    from precis.pcb.datasheets import datasheet_state

    # 'older' holds the newer link — recency beats the lower ref_id.
    assert datasheet_state(store, "C25804")["datasheet"] == (older, "olderds")


def test_datasheet_states_stub_never_primary(store: Store) -> None:
    """A bodiless stub minted after a filled sheet (``_put_stub`` allows
    it) holds the newer link — ``pdf_sha256 IS NULL`` must still sort
    last, so the stub never takes the primary slot."""
    pid = store.ensure_part_ref("C25804", uncatalogued_ok=True)
    filled = _ds(store, cite_key="filledds", title="filled sheet")
    stub = _ds(store, cite_key="stubds", title="stub sheet")
    store.add_link(src_ref_id=filled, dst_ref_id=pid, relation="datasheet-of")
    store.add_link(src_ref_id=stub, dst_ref_id=pid, relation="datasheet-of")
    sha = f"{filled:064d}"
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO pdfs (pdf_sha256, content_hash, page_count, "
            "size_bytes, storage_path) VALUES (%s, %s, 1, 100, '/tmp/held') "
            "ON CONFLICT (pdf_sha256) DO NOTHING",
            (sha, sha),
        )
        conn.execute(
            "UPDATE refs SET pdf_sha256 = %s WHERE ref_id = %s",
            (sha, filled),
        )
        # The stub holds the newer link — recency alone would pick it.
        conn.execute(
            "UPDATE links SET created_at = '2026-01-01' WHERE src_ref_id = %s",
            (filled,),
        )
        conn.execute(
            "UPDATE links SET created_at = '2026-06-01' WHERE src_ref_id = %s",
            (stub,),
        )
        conn.commit()
    from precis.pcb.datasheets import datasheet_state

    assert datasheet_state(store, "C25804")["datasheet"] == (filled, "filledds")
