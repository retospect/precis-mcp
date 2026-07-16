"""Off-site lookup link builders (``precis_web.paper_links``)."""

from __future__ import annotations

from precis_web.paper_links import (
    arxiv_pdf_url,
    doi_url,
    libkey_url,
    scholar_url,
    uol_url,
)

# A gnarly legacy Wiley DOI with reserved chars (< > ( ) : ;) that must
# be percent-encoded to make a well-formed URL.
_WILEY = "10.1002/(sici)1521-3765(19991105)5:11<3310::aid-chem3310>3.0.co;2-r"


def test_libkey_url_encodes_doi_keeps_slash() -> None:
    url = libkey_url(_WILEY)
    # Library-specific form: libraries/<id>/<DOI>. The DOI's own slash
    # stays a path separator; the other reserved chars are encoded.
    assert url.startswith("https://libkey.io/libraries/2545/10.1002/")
    assert "%28sici%29" in url  # ( ) encoded
    assert "%3C3310" in url  # < encoded
    assert "<" not in url and ">" not in url and ";" not in url


def test_libkey_url_simple_doi() -> None:
    assert libkey_url("10.1038/nphys1170") == (
        "https://libkey.io/libraries/2545/10.1038/nphys1170"
    )


def test_libkey_url_non_doi_is_empty() -> None:
    # arXiv preprints have a free PDF; an opaque S2 hash isn't a LibKey
    # key. Neither gets a LibKey link.
    assert libkey_url("arxiv:2401.00001") == ""
    assert libkey_url("s2:deadbeef") == ""
    assert libkey_url("") == ""


def test_arxiv_pdf_url() -> None:
    # arXiv identifier → direct PDF; old-style ids keep their slash.
    assert arxiv_pdf_url("arxiv:2401.12345") == "https://arxiv.org/pdf/2401.12345"
    assert (
        arxiv_pdf_url("arxiv:cond-mat/0410550")
        == "https://arxiv.org/pdf/cond-mat/0410550"
    )
    # Non-arXiv identifiers get no arXiv link.
    assert arxiv_pdf_url("10.1038/nphys1170") == ""
    assert arxiv_pdf_url("s2:deadbeef") == ""
    assert arxiv_pdf_url("") == ""


def test_libkey_and_arxiv_are_mutually_exclusive() -> None:
    # A given identifier feeds exactly one direct-download builder.
    doi, arx = "10.1038/nphys1170", "arxiv:2401.12345"
    assert libkey_url(doi) and not arxiv_pdf_url(doi)
    assert arxiv_pdf_url(arx) and not libkey_url(arx)


def test_other_builders_unchanged_for_doi() -> None:
    # Sanity: the DOI still drives the publisher, Primo, and Scholar
    # links (guards against a refactor accidentally narrowing them).
    assert doi_url("10.1038/nphys1170") == "https://doi.org/10.1038/nphys1170"
    assert "uol.primo.exlibrisgroup.com" in uol_url("10.1038/nphys1170")
    assert "scholar.google.com" in scholar_url("10.1038/nphys1170")
