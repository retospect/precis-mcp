"""``identity_verdict``: related-article citations are not identity (gr513878).

A companion journal's front page (MethodsX, Data in Brief) typesets the
*related* paper's title and DOI inside a "Related research article" box.
A fetched companion PDF for a stub therefore carries the stub's own DOI
and restates its title — inside a citation to it — while the body is a
different paper. Neither the cited DOI nor the cited title may prove
identity.
"""

from precis.ingest.pdf_identity import identity_verdict

_STUB_DOI = "10.1016/j.mimet.2018.11.020"
_STUB_TITLE = (
    "Xylenol orange-based loop-mediated DNA isothermal amplification "
    "for sensitive naked-eye detection of Escherichia coli"
)

#: The incident shape: a MethodsX first page — its own title and DOI,
#: plus a related-article box citing the stub paper by title, journal
#: and DOI.
_COMPANION_PAGE = (
    "A lyophilization-compatible colorimetric LAMP assay for cold "
    "chain-free, extraction-free and rapid post-enrichment detection "
    "of E. coli\n"
    "MethodsX 12 (2026) 104138\n"
    "Related research article: W. Jaroenram, P. Cecere, P.P. Pompa, "
    "Xylenol orange-based loop-mediated DNA isothermal amplification "
    "for sensitive naked-eye detection of Escherichia coli, "
    "J. Microbiol. Methods 156 (2019), "
    "https://doi.org/10.1016/j.mimet.2018.11.020.\n"
    "journal homepage: www.elsevier.com/locate/mex\n"
    "DOI 10.1016/j.mex.2026.104138"
)


def test_related_article_doi_is_not_identity():
    """gr513878: the stub DOI inside a related-article box must not
    return ``"doi"`` — the fetch gate's early accept would otherwise
    admit the companion PDF whose citation mentions it."""
    verdict, _ = identity_verdict(
        [_COMPANION_PAGE, "methods text", "results text"],
        title=_STUB_TITLE,
        doi=_STUB_DOI,
    )
    assert verdict == "mismatch"


def test_cited_title_does_not_confirm():
    """The box restates the cited title verbatim, so the title-word
    overlap must not see it either — without the elision this is a
    ``"title"`` accept of the wrong paper."""
    verdict, _ = identity_verdict(
        [_COMPANION_PAGE, "x", "y"], title=_STUB_TITLE, doi="10.9/absent"
    )
    assert verdict == "mismatch"


def test_cited_only_doi_short_title_mismatch():
    """No title to compare (fewer than two significant words): a DOI
    seen only inside a citation is a mismatch, and the detail says so."""
    verdict, detail = identity_verdict(
        [_COMPANION_PAGE, "x", "y"], title="X", doi=_STUB_DOI
    )
    assert verdict == "mismatch"
    assert "related-article" in (detail or "")


def test_doi_outside_the_box_still_confirms():
    """A second occurrence of the stub DOI outside the citation counts —
    the related-article span ends at the DOI that closes it."""
    page = _COMPANION_PAGE + "\nDOI 10.1016/j.mimet.2018.11.020"
    verdict, _ = identity_verdict([page, "x", "y"], title=_STUB_TITLE, doi=_STUB_DOI)
    assert verdict == "doi"


def test_own_identity_still_passes():
    """The right paper: its own title and DOI on page 1, no related
    box — verdict unchanged."""
    own = (
        _STUB_TITLE + "\n"
        "journal homepage: www.elsevier.com/locate/mimet\n"
        "DOI 10.1016/j.mimet.2018.11.020"
    )
    verdict, _ = identity_verdict([own, "x", "y"], title=_STUB_TITLE, doi=_STUB_DOI)
    assert verdict == "doi"
