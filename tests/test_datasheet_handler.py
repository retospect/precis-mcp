"""DatasheetHandler — the evidence-role sibling of PaperHandler (ADR 0042 §7).

Verifies the *declared* differences from paper (corpus_role, no put,
restricted views, handle code) plus the datasheet-of relation wiring, without
ingesting a real PDF. The shared get/search machinery is exercised by the
paper handler tests; here we only assert the datasheet-specific spec + a
registration smoke test."""

from __future__ import annotations

from precis.dispatch import Hub
from precis.handlers.datasheet import DatasheetHandler
from precis.handlers.paper import PaperHandler
from precis.store import Store
from precis.store.types import _INVERSE_RELATIONS
from precis.utils import handle_registry


def test_datasheet_is_an_evidence_role_document() -> None:
    assert DatasheetHandler.spec.kind == "datasheet"
    # Unlike a cfp (spec), a datasheet *is* citable — but in its own kind so
    # it never mixes into academic paper search.
    assert DatasheetHandler.spec.corpus_role == "evidence"


def test_datasheet_subclasses_paper_for_dry_reuse() -> None:
    assert issubclass(DatasheetHandler, PaperHandler)


def test_datasheet_has_no_put_and_no_citation_export_views(hub: Hub) -> None:
    # A datasheet is acquired by ingesting a PDF, not by minting a stub.
    assert DatasheetHandler.spec.supports_put is False
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


def test_datasheet_empty_search_names_datasheet_not_paper(store: Store) -> None:
    """Regression: datasheet subclasses PaperHandler and reuses its search
    path, whose empty-result branch once hardcoded the noun as "no paper
    blocks match" — leaking the *paper* kind on a datasheet miss."""
    lex_only = DatasheetHandler(hub=Hub(store=store))
    resp = lex_only.search(q="zzqqxx-no-such-token")
    assert "no datasheet blocks match" in resp.body
    assert "paper" not in resp.body
