"""CfpHandler — the spec-role sibling of PaperHandler (proposal writing).

Verifies the *declared* differences from paper (corpus_role, no put,
restricted views, handle code) without ingesting a real PDF. The shared
get/search machinery is exercised by the paper handler tests; here we
only assert the cfp-specific spec + a registration smoke test."""

from __future__ import annotations

from precis.dispatch import Hub
from precis.handlers.cfp import CfpHandler
from precis.handlers.paper import PaperHandler
from precis.store import Store
from precis.utils import handle_registry


def test_cfp_is_a_spec_role_document() -> None:
    assert CfpHandler.spec.kind == "cfp"
    assert CfpHandler.spec.corpus_role == "spec"
    # Paper is evidence, cfp is spec — the anti-citation distinction.
    assert PaperHandler.spec.corpus_role == "evidence"


def test_cfp_subclasses_paper_for_dry_reuse() -> None:
    assert issubclass(CfpHandler, PaperHandler)


def test_cfp_has_no_put_and_no_citation_export_views(hub: Hub) -> None:
    # A CFP is acquired by ingesting a PDF, not by minting a stub.
    assert CfpHandler.spec.supports_put is False
    # Reading verbs are inherited.
    assert CfpHandler.spec.supports_get is True
    assert CfpHandler.spec.supports_search is True
    # Citation-export views are dropped (a spec is never a bib entry).
    views = CfpHandler(hub=hub).accepted_views()
    assert "bibtex" not in views
    assert "ris" not in views
    assert "bibliography" not in views
    # …but the reading views carry over.
    assert "toc" in views
    assert "abstract" in views


def test_cfp_handle_codes_registered() -> None:
    assert handle_registry.code_for_kind("cfp") == "cf"
    assert handle_registry.code_for_kind("cfp", chunk=True) == "qc"
    # Round-trips through the universal handle parser.
    kind, is_chunk = handle_registry.kind_for_code("cf")
    assert kind == "cfp" and is_chunk is False


def test_cfp_empty_search_names_cfp_not_paper(store: Store) -> None:
    """Regression: cfp (and datasheet) subclass PaperHandler and reuse
    its search path, whose empty-result branch hardcoded the noun as
    "no paper blocks match" — leaking the *paper* kind on a cfp miss.
    The empty-result noun must be the actual searched kind, matching the
    per-kind convention every other block handler follows (tex/web/
    markdown/plaintext/perplexity all say "no <kind> blocks match")."""
    lex_only = CfpHandler(hub=Hub(store=store))
    resp = lex_only.search(q="zzqqxx-no-such-token")
    assert "no cfp blocks match" in resp.body
    assert "paper" not in resp.body
