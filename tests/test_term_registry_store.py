"""Store-level tests for the structured term registry — ``defined_terms``,
``ensure_registry_heading`` (lookup / adopt / reconcile), and
``parts_callout_map``. Real Postgres (the ``hub`` fixture)."""

from __future__ import annotations

import re

import pytest

from precis.dispatch import Hub
from precis.handlers.draft import DraftHandler


@pytest.fixture
def draft(hub: Hub) -> DraftHandler:
    return DraftHandler(hub=hub)


def _dc(body: str) -> str:
    m = re.search(r"dc\d+", body)
    assert m is not None, f"no dc handle in {body!r}"
    return m.group(0)


def _proj(hub: Hub) -> int:
    return hub.live_store.insert_ref(kind="todo", slug=None, title="Proj").id


def _mk(hub: Hub, draft: DraftHandler) -> int:
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    ref = hub.live_store.get_ref(kind="draft", id="nt")
    assert ref is not None
    return ref.id


def _headings(hub: Hub, ref_id: int) -> list:
    return [
        c
        for c in hub.live_store.drafts.reading_order(ref_id)
        if c.chunk_kind == "heading"
    ]


# ── defined_terms ────────────────────────────────────────────────────────


def test_defined_terms_part_surfaces_all_key_to_rich_entry(
    draft: DraftHandler, hub: Hub
) -> None:
    ref_id = _mk(hub, draft)
    gloss = hub.live_store.drafts.ensure_registry_heading(ref_id, "components")
    hub.live_store.drafts.add_chunks(
        ref_id=ref_id,
        chunk_kind="term",
        text="an operational amplifier",
        at={"into": gloss, "last": True},
        meta={
            "short": "op-amp",
            "surface_forms": ["LM358"],
            "mpn": "LM358DR",
            "manufacturer": "Texas Instruments",
            "url": "https://example.com/lm358.pdf",
            "registry": "components",
        },
    )
    terms = hub.live_store.drafts.defined_terms(ref_id)
    # Every string surface reaches the same rich record.
    for surface in ("op-amp", "LM358", "LM358DR"):
        assert surface in terms, surface
        e = terms[surface]
        assert e["definition"] == "an operational amplifier"
        assert e["mpn"] == "LM358DR"
        assert e["manufacturer"] == "Texas Instruments"
        assert e["url"].endswith("lm358.pdf")
        assert e["registry"] == "components"


def test_defined_terms_abbrev_is_a_dedicated_resolvable_surface(
    draft: DraftHandler, hub: Hub
) -> None:
    """gripe 56690: a term's dedicated ``abbrev`` (parallel to ``short``, but
    a distinct field) is itself a resolvable surface routing to the same
    record — the primary label can be the long descriptive form while the
    acronym still hover-resolves in prose."""
    ref_id = _mk(hub, draft)
    draft.put(
        id="nt",
        chunk_kind="term",
        text="stereolithography",
        meta={"short": "stereolithography", "abbrev": "STL"},
    )
    terms = hub.live_store.drafts.defined_terms(ref_id)
    for surface in ("stereolithography", "STL"):
        assert surface in terms, surface
        e = terms[surface]
        assert e["definition"] == "stereolithography"
        assert e["abbrev"] == "STL"


def test_defined_terms_plain_glossary_has_no_bag(draft: DraftHandler, hub: Hub) -> None:
    ref_id = _mk(hub, draft)
    draft.put(
        id="nt",
        chunk_kind="term",
        text="metal-organic framework",
        meta={"short": "MOF"},
    )
    terms = hub.live_store.drafts.defined_terms(ref_id)
    assert terms["MOF"]["definition"] == "metal-organic framework"
    # No manufacturing attribute bag on a plain glossary term.
    assert "mpn" not in terms["MOF"]
    assert "manufacturer" not in terms["MOF"]
    assert "url" not in terms["MOF"]


def test_defined_terms_explicit_wins_over_inline(draft: DraftHandler, hub: Hub) -> None:
    ref_id = _mk(hub, draft)
    title_h = hub.live_store.drafts.reading_order(ref_id)[0].handle
    draft.put(
        id="nt",
        chunk_kind="paragraph",
        text="We use polyethyleneimine (PEI) here.",
        at={"after": "¶" + title_h},
    )
    draft.put(
        id="nt", chunk_kind="term", text="a different expansion", meta={"short": "PEI"}
    )
    terms = hub.live_store.drafts.defined_terms(ref_id)
    assert terms["PEI"]["definition"] == "a different expansion"


# ── ensure_registry_heading ──────────────────────────────────────────────


def test_registry_heading_is_created_and_reused_per_role(
    draft: DraftHandler, hub: Hub
) -> None:
    ref_id = _mk(hub, draft)
    g1 = hub.live_store.drafts.ensure_registry_heading(ref_id, "glossary")
    g2 = hub.live_store.drafts.ensure_registry_heading(ref_id, "glossary")
    comp = hub.live_store.drafts.ensure_registry_heading(ref_id, "components")
    assert g1 == g2  # same home reused, not duplicated
    assert comp != g1  # a distinct registry gets its own home
    titles = {c.text for c in _headings(hub, ref_id)}
    assert "Glossary" in titles and "Components" in titles


def test_registry_heading_adopts_legacy_text_heading(
    draft: DraftHandler, hub: Hub
) -> None:
    """A renamed/imported 'Abbreviations' heading is adopted (stamped
    meta.registry) — no second 'Glossary' is minted (the two-cluster bug)."""
    ref_id = _mk(hub, draft)
    created = hub.live_store.drafts.add_chunks(
        ref_id=ref_id,
        chunk_kind="heading",
        text="Abbreviations",
        at={"last": True},
    )
    legacy_dc = created[0].dc
    got = hub.live_store.drafts.ensure_registry_heading(ref_id, "glossary")
    assert got == legacy_dc  # adopted, not replaced
    headings = _headings(hub, ref_id)
    # No fresh "Glossary" heading minted.
    assert not any(h.text == "Glossary" for h in headings)
    adopted = next(h for h in headings if h.text == "Abbreviations")
    assert (adopted.meta or {}).get("registry") == "glossary"


def test_registry_heading_reconciles_duplicates(draft: DraftHandler, hub: Hub) -> None:
    """Two role-tagged headings fold to one; the straggler's leaves reparent
    under the earliest-pos canonical (belt-and-suspenders reconcile)."""
    ref_id = _mk(hub, draft)
    first = hub.live_store.drafts.add_chunks(
        ref_id=ref_id,
        chunk_kind="heading",
        text="Glossary",
        at={"last": True},
        meta={"registry": "glossary"},
    )[0]
    second = hub.live_store.drafts.add_chunks(
        ref_id=ref_id,
        chunk_kind="heading",
        text="Glossary (dup)",
        at={"last": True},
        meta={"registry": "glossary"},
    )[0]
    term = hub.live_store.drafts.add_chunks(
        ref_id=ref_id,
        chunk_kind="term",
        text="a def",
        at={"into": second.dc, "last": True},
        meta={"short": "X"},
    )[0]
    # Triggers the reconcile.
    canonical = hub.live_store.drafts.ensure_registry_heading(ref_id, "glossary")
    assert canonical == first.dc
    live_headings = _headings(hub, ref_id)
    glossary_homes = [
        h for h in live_headings if (h.meta or {}).get("registry") == "glossary"
    ]
    assert len(glossary_homes) == 1  # the duplicate is retired
    moved = hub.live_store.drafts.get_draft_chunk(term.handle)
    assert moved is not None and moved.parent_chunk_id == first.chunk_id


# ── parts_callout_map ────────────────────────────────────────────────────


def test_parts_callout_map_is_spaced_and_reorder_recomputes(
    draft: DraftHandler, hub: Hub
) -> None:
    ref_id = _mk(hub, draft)
    home = hub.live_store.drafts.ensure_registry_heading(ref_id, "parts")
    parts = [
        hub.live_store.drafts.add_chunks(
            ref_id=ref_id,
            chunk_kind="term",
            text=f"part {name}",
            at={"into": home, "last": True},
            meta={"short": name, "registry": "parts"},
        )[0]
        for name in ("housing", "shaft", "cap")
    ]
    from precis.utils import handle_registry

    dcs = [handle_registry.normalize(p.dc) for p in parts]
    m = hub.live_store.drafts.parts_callout_map(ref_id, "parts")
    assert m[dcs[0]] == 100 and m[dcs[1]] == 105 and m[dcs[2]] == 110
    # Reorder: move the last part to the front → numerals recompute positionally.
    hub.live_store.drafts.move_chunk(parts[2].handle, {"into": home, "first": True})
    m2 = hub.live_store.drafts.parts_callout_map(ref_id, "parts")
    assert m2[dcs[2]] == 100 and m2[dcs[0]] == 105 and m2[dcs[1]] == 110


def test_parts_callout_map_empty_for_non_render_registry(
    draft: DraftHandler, hub: Hub
) -> None:
    ref_id = _mk(hub, draft)
    assert hub.live_store.drafts.parts_callout_map(ref_id, "components") == {}
    assert hub.live_store.drafts.parts_callout_map(ref_id, "glossary") == {}


# ── handler: put routing + insert-callout freeze, edit bag ────────────────


def test_put_components_term_stamps_registry_and_freezes_callout(
    draft: DraftHandler, hub: Hub
) -> None:
    ref_id = _mk(hub, draft)
    r1 = draft.put(
        id="nt",
        chunk_kind="term",
        text="op-amp",
        meta={"registry": "components", "short": "LM358", "mpn": "LM358DR"},
    )
    r2 = draft.put(
        id="nt",
        chunk_kind="term",
        text="regulator",
        meta={"registry": "components", "short": "LDO"},
    )
    c1 = hub.live_store.drafts.get_draft_chunk(_dc(r1.body))
    c2 = hub.live_store.drafts.get_draft_chunk(_dc(r2.body))
    assert c1 is not None and c2 is not None
    assert c1.meta["registry"] == "components" and c1.meta["callout"] == 1
    assert c2.meta["callout"] == 2  # consecutive
    assert c1.meta["mpn"] == "LM358DR"


def test_put_components_term_files_under_components_home(
    draft: DraftHandler, hub: Hub
) -> None:
    ref_id = _mk(hub, draft)
    r = draft.put(
        id="nt",
        chunk_kind="term",
        text="op-amp",
        meta={"registry": "components", "short": "LM358"},
    )
    leaf = hub.live_store.drafts.get_draft_chunk(_dc(r.body))
    assert leaf is not None
    parent = next(
        c
        for c in hub.live_store.drafts.reading_order(ref_id)
        if c.chunk_id == leaf.parent_chunk_id
    )
    assert parent.chunk_kind == "heading"
    assert (parent.meta or {}).get("registry") == "components"


def test_put_glossary_term_gets_no_callout(draft: DraftHandler, hub: Hub) -> None:
    ref_id = _mk(hub, draft)
    r = draft.put(
        id="nt",
        chunk_kind="term",
        text="metal-organic framework",
        meta={"short": "MOF"},
    )
    leaf = hub.live_store.drafts.get_draft_chunk(_dc(r.body))
    assert leaf is not None
    assert leaf.meta["registry"] == "glossary"
    assert "callout" not in leaf.meta


def test_edit_meta_patches_term_attribute_bag(draft: DraftHandler, hub: Hub) -> None:
    ref_id = _mk(hub, draft)
    r = draft.put(
        id="nt",
        chunk_kind="term",
        text="op-amp",
        meta={"registry": "components", "short": "LM358"},
    )
    dc = _dc(r.body)
    draft.edit(
        id=dc,
        meta={"mpn": "LM358DR", "manufacturer": "TI", "url": "https://x/lm358.pdf"},
    )
    leaf = hub.live_store.drafts.get_draft_chunk(dc)
    assert leaf is not None
    assert leaf.meta["mpn"] == "LM358DR"
    assert leaf.meta["manufacturer"] == "TI"
    # The frozen callout survives a bag edit.
    assert leaf.meta["callout"] == 1
