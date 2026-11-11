"""Draft concept-label lint (gr626720) — in-house labels in paper drafts.

An in-house surface — a live ``concept`` with no ``derived-from`` →
paper/patent provenance, a ``proposed`` taxon that hasn't earned
``systematic``, or a node carrying the ``(lit: …)`` canonical marker —
used in draft prose is flagged when a literature canonical term exists
or no definition is supplied; deliberately introduced terms with
definitions are allowed, and ambiguous equivalents flag for review
instead of picking a side. Exercises the write-path hint through the
draft handler (real ``store``/``hub`` fixtures).
"""

from __future__ import annotations

import uuid

import pytest

from precis.dispatch import Hub
from precis.handlers.draft import DraftHandler


@pytest.fixture
def draft(hub: Hub) -> DraftHandler:
    return DraftHandler(hub=hub)


def _u() -> str:
    return uuid.uuid4().hex[:8]


def _proj(hub: Hub) -> int:
    return hub.live_store.insert_ref(kind="todo", slug=None, title="Proj").id


def _order(hub: Hub, slug: str) -> list:
    ref = hub.live_store.get_ref(kind="draft", id=slug)
    assert ref is not None
    return hub.live_store.drafts.reading_order(ref.id)


def _concept(
    hub: Hub,
    name: str,
    *,
    definition: str = "",
    aliases: list[str] | None = None,
    paper_id: int | None = None,
) -> int:
    from precis.reading.promote import create_concept

    return create_concept(
        hub.live_store,
        name=name,
        definition=definition,
        aliases=aliases,
        source_paper_id=paper_id,
    )


def _paper(hub: Hub, title: str = "source paper") -> int:
    from tests.workers._helpers import seed_ref

    return seed_ref(hub.live_store, title=title)


def _taxon(
    hub: Hub, name: str, *, definition: str = "", status: str = "proposed"
) -> int:
    from precis.taxonomy.nodes import initial_taxon_meta

    return hub.live_store.insert_ref(
        kind="taxon",
        slug=None,
        title=name,
        meta=initial_taxon_meta(name, definition, extra={"status": status}),
    ).id


def _mk_draft(draft: DraftHandler, hub: Hub, slug: str = "nt") -> str:
    proj = _proj(hub)
    draft.put(id=slug, title="T", project=proj)
    return _order(hub, slug)[0].handle


def _para(draft: DraftHandler, hub: Hub, text: str, slug: str = "nt") -> str:
    title_h = (
        _mk_draft(draft, hub, slug) if slug == "nt" else _order(hub, slug)[0].handle
    )
    r = draft.put(
        id=slug,
        chunk_kind="paragraph",
        text=text,
        at={"after": "¶" + title_h},
    )
    return r.body


class TestConceptLabelHint:
    def test_unexplained_inhouse_label_flags(
        self, draft: DraftHandler, hub: Hub
    ) -> None:
        label = f"tazmofit-{_u()}"
        _concept(hub, label)
        body = _para(draft, hub, f"The {label} binds at low potential.")
        assert "unexplained in-house label" in body
        assert label in body

    def test_defined_inhouse_label_allowed_by_concept_def(
        self, draft: DraftHandler, hub: Hub
    ) -> None:
        label = f"glarch-{_u()}"
        _concept(hub, label, definition="a made-up solvent cage")
        body = _para(draft, hub, f"The {label} binds at low potential.")
        assert "in-house label" not in body

    def test_defined_inhouse_label_allowed_by_glossary(
        self, draft: DraftHandler, hub: Hub
    ) -> None:
        """A concept node with no definition stays flagged until the draft
        itself defines the surface in a glossary ``term`` leaf."""
        label = f"snipgle-{_u()}"
        _concept(hub, label)
        title_h = _mk_draft(draft, hub)
        draft.put(
            id="nt",
            chunk_kind="term",
            text="a deliberately coined jig",
            meta={"short": label},
        )
        r = draft.put(
            id="nt",
            chunk_kind="paragraph",
            text=f"The {label} binds at low potential.",
            at={"after": "¶" + title_h},
        )
        assert "in-house label" not in r.body

    def test_lit_sourced_concept_clean(self, draft: DraftHandler, hub: Hub) -> None:
        """A concept promoted from a paper (``derived-from``) is
        literature-sourced — not an in-house label."""
        label = f"perovskage-{_u()}"
        _concept(hub, label, paper_id=_paper(hub))
        body = _para(draft, hub, f"The {label} mechanism is well studied.")
        assert "in-house label" not in body

    def test_systematic_taxon_label_clean(self, draft: DraftHandler, hub: Hub) -> None:
        """A taxon that earned ``systematic`` is literature canonical —
        never an in-house flag."""
        label = f"measuron-{_u()}"
        _taxon(hub, label, status="systematic")
        body = _para(draft, hub, f"The {label} dominates the response.")
        assert "in-house label" not in body

    def test_paper_sourced_taxon_label_clean(
        self, draft: DraftHandler, hub: Hub
    ) -> None:
        """A ``proposed`` taxon carrying a ``derived-from`` → paper edge
        is literature-sourced — provenance, not status alone."""
        label = f"sourcedtax-{_u()}"
        tid = _taxon(hub, label)
        hub.live_store.add_link(
            src_ref_id=tid, dst_ref_id=_paper(hub), relation="derived-from"
        )
        body = _para(draft, hub, f"The {label} dominates the response.")
        assert "in-house label" not in body

    def test_proposed_taxon_label_flags_unexplained(
        self, draft: DraftHandler, hub: Hub
    ) -> None:
        """A ``proposed`` taxon is an in-house proposal, not a
        canonical — with no definition it flags like an unsourced
        concept."""
        label = f"protazon-{_u()}"
        _taxon(hub, label)
        body = _para(draft, hub, f"The {label} dominates the response.")
        assert "unexplained in-house label" in body

    def test_proposed_taxon_defined_allowed(
        self, draft: DraftHandler, hub: Hub
    ) -> None:
        """A ``proposed`` taxon carrying a definition is a deliberately
        introduced term — clean."""
        label = f"draftaxon-{_u()}"
        _taxon(hub, label, definition="a candidate measurand under review")
        body = _para(draft, hub, f"The {label} dominates the response.")
        assert "in-house label" not in body

    def test_same_surface_collision_flags_ambiguous(
        self, draft: DraftHandler, hub: Hub
    ) -> None:
        """A surface claimed by both an in-house concept and a canonical
        node is an ambiguous equivalent — flagged for review, not renamed."""
        label = f"volticell-{_u()}"
        _concept(hub, label)
        _taxon(hub, label, status="systematic")
        body = _para(draft, hub, f"The {label} geometry controls the rate.")
        assert "ambiguous label" in body
        assert label in body

    def test_other_surface_canonical_flags(self, draft: DraftHandler, hub: Hub) -> None:
        """The draft uses the in-house name while a canonical node claims
        the concept's own alias — a literature canonical exists."""
        label, canon = f"tazx-{_u()}", f"zorbax-{_u()}"
        _concept(hub, label, aliases=[canon])
        _taxon(hub, canon, status="systematic")
        body = _para(draft, hub, f"We rely on {label} throughout.")
        assert "literature canonical" in body
        assert canon in body

    def test_lit_concept_via_other_surface_flags_canonical(
        self, draft: DraftHandler, hub: Hub
    ) -> None:
        """The canonical claimant may sit on the in-house node's *other*
        surface — provenance is probed on the alias reach too, so a
        lit-sourced concept there still reads as the literature word."""
        label, canon = f"hausword-{_u()}", f"litword-{_u()}"
        _concept(hub, label, aliases=[canon])
        _concept(hub, canon, paper_id=_paper(hub))
        body = _para(draft, hub, f"We rely on {label} throughout.")
        assert "literature canonical" in body
        assert canon in body

    def test_proposed_taxon_via_other_surface_not_canonical(
        self, draft: DraftHandler, hub: Hub
    ) -> None:
        """A ``proposed`` taxon on the alias is no literature canonical —
        no false "prefer it" suggestion; the undefined concept flags as
        unexplained instead."""
        label, canon = f"hauswort-{_u()}", f"proptax-{_u()}"
        _concept(hub, label, aliases=[canon])
        _taxon(hub, canon)
        body = _para(draft, hub, f"We rely on {label} throughout.")
        assert "literature canonical" not in body
        assert "unexplained in-house label" in body

    def test_lit_marker_flags_canonical(self, draft: DraftHandler, hub: Hub) -> None:
        """A ``(lit: …)`` marker on the node's own definition names the
        literature word — flagged even though a definition exists."""
        label = f"houseword-{_u()}"
        _concept(hub, label, definition="our coined jig (lit: clamp)")
        body = _para(draft, hub, f"The {label} holds the plate.")
        assert "literature canonical" in body
        assert "clamp" in body

    def test_alias_surface_detected(self, draft: DraftHandler, hub: Hub) -> None:
        """The used label may be the alias; the node's name still shows."""
        name, alias = f"canoname-{_u()}", f"shorttag-{_u()}"
        _concept(hub, name, aliases=[alias])
        body = _para(draft, hub, f"Every {alias} needs a primer.")
        assert "unexplained in-house label" in body
        assert alias in body

    def test_edit_does_not_renag_preexisting_label(
        self, draft: DraftHandler, hub: Hub
    ) -> None:
        """Editing a chunk that already used the label doesn't re-nag —
        only a label *this write* introduced fires."""
        label = f"plinth-{_u()}"
        _concept(hub, label)
        body = _para(draft, hub, f"The {label} sits under the arch.")
        assert "unexplained in-house label" in body
        para = next(c for c in _order(hub, "nt") if label in c.text)
        r = draft.edit(id=para.dc, text=f"The {label} sits under the arch, still.")
        assert "in-house label" not in r.body

    def test_plain_prose_without_term_hits_is_clean(
        self, draft: DraftHandler, hub: Hub
    ) -> None:
        body = _para(draft, hub, "An ordinary sentence with no coined terms.")
        assert "in-house label" not in body

    def test_non_prose_chunk_kind_skips(self, store, hub: Hub) -> None:
        """Function-level: a ``term``/``code`` chunk kind is a definition
        or verbatim payload — the lint never scans it."""
        from precis.draft.lint import concept_label_hint

        label = f"quib-{_u()}"
        _concept(hub, label)
        for kind in ("term", "code", "heading"):
            assert (
                concept_label_hint(
                    store, "nt", 0, f"The {label} again.", chunk_kind=kind
                )
                == ""
            )
