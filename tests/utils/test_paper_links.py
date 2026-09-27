"""Off-site lookup link builders (``precis.utils.paper_links``).

Core home of the builders re-exported by ``precis_web.paper_links``
(covered separately in ``tests/precis_web/test_paper_links.py``); this
file just guards the core module directly, including that it stays
``precis_web``-free so export can depend on it.
"""

from __future__ import annotations

import ast
from pathlib import Path

from precis.utils.paper_links import doi_url, libkey_url, uol_url


def test_doi_url_variants() -> None:
    assert doi_url("10.1038/nphys1170") == "https://doi.org/10.1038/nphys1170"
    assert doi_url("arxiv:2401.12345") == "https://arxiv.org/abs/2401.12345"
    assert doi_url("s2:deadbeef") == ""
    assert doi_url("") == ""


def test_doi_url_percent_encodes_reserved_chars() -> None:
    # A legacy Wiley-style DOI: '/' ':' '(' ')' stay bare (readable, and
    # '/' must stay a path separator); '<' '>' ';' are reserved-URL chars
    # that must be percent-encoded so the link is well-formed.
    wiley = "10.1002/(SICI)1097-0258(19980815/30)17:15/16<1661::AID-SIM968>3.0.CO;2-2"
    url = doi_url(wiley)
    assert url.startswith(
        "https://doi.org/10.1002/(SICI)1097-0258(19980815/30)17:15/16"
    )
    assert "%3C1661" in url  # '<' encoded
    assert "%3E3.0" in url  # '>' encoded
    assert "%3B2-2" in url  # ';' encoded
    assert "<" not in url and ">" not in url and ";" not in url


def test_uol_url_doi_and_arxiv() -> None:
    url = uol_url("10.1038/nphys1170")
    assert "uol.primo.exlibrisgroup.com" in url
    assert "any,contains,10.1038%2Fnphys1170" in url
    assert uol_url("s2:deadbeef") == ""
    assert uol_url("") == ""


def test_uol_url_default_carries_ul_vid() -> None:
    # The default template (no override) resolves through PrecisConfig's
    # default, which is the University of Limerick's Primo tenant/view.
    assert "vid=353UOL_INST:353UOL_VU1" in uol_url("10.1038/nphys1170")


def test_uol_url_search_url_template_override() -> None:
    # An explicit template wins over the config default, and the {query}
    # placeholder is filled with the percent-encoded search token.
    url = uol_url(
        "10.1038/nphys1170",
        search_url_template="https://example.org/find?q={query}",
    )
    assert url == "https://example.org/find?q=10.1038%2Fnphys1170"


def test_uol_url_template_with_unrelated_literal_braces() -> None:
    # ``.replace`` (not ``str.format``) fills {query} without choking on a
    # second literal brace pair the operator's URL happens to contain —
    # config's own field_validator rejects a *second* {query}, but nothing
    # stops an unrelated ``{foo}`` segment from surviving untouched.
    url = uol_url(
        "10.1038/nphys1170",
        search_url_template="https://example.org/find?q={query}&tag={foo}",
    )
    assert url == "https://example.org/find?q=10.1038%2Fnphys1170&tag={foo}"


def test_libkey_url_doi_only() -> None:
    assert libkey_url("10.1038/nphys1170") == (
        "https://libkey.io/libraries/2545/10.1038/nphys1170"
    )
    assert libkey_url("arxiv:2401.00001") == ""


def test_libkey_url_library_id_override() -> None:
    assert libkey_url("10.1038/nphys1170", library_id="9999") == (
        "https://libkey.io/libraries/9999/10.1038/nphys1170"
    )


def test_core_module_does_not_import_precis_web() -> None:
    repo_root = Path(__file__).resolve().parent.parent.parent
    src = (repo_root / "src/precis/utils/paper_links.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("precis_web")
        elif isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("precis_web")
