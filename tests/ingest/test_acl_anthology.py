"""ACL DOI extraction and path-safe PDF URL construction; no network."""

import pytest

from precis.ingest.acl_anthology import anthology_id_from_doi, pdf_url


@pytest.mark.parametrize(
    ("doi", "expected"),
    [
        ("10.18653/v1/2023.acl-long.1", "2023.acl-long.1"),
        ("10.18653/v1/2020.acl.1", "2020.acl.1"),
        ("10.18653/v1/P18-1001", "P18-1001"),
        ("10.18653/v1/W04-1013", "W04-1013"),
        ("10.18653/V1/2023.ACL-long.1", "2023.acl-long.1"),
        ("10.18653/V1/p18-1001", "P18-1001"),
        ("10.3115/123456.123457", None),
        ("10.186530/v1/2023.acl-long.1", None),
        ("10.18653/v2/2023.acl-long.1", None),
        ("10.18653/v1/../../other", None),
        ("10.18653/v1/2023.acl-long.1/../../other", None),
        ("10.18653/v1/%2e%2e%2fother", None),
        ("10.18653/v1/2023.acl-long.1?download=other", None),
        ("10.18653/v1/2023.acl-long.1#other", None),
        ("10.18653/v1/2023.acl-long.1\n", None),
        ("10.18653/v1/", None),
        ("", None),
    ],
)
def test_anthology_id_from_doi(doi: str, expected: str | None) -> None:
    assert anthology_id_from_doi(doi) == expected


@pytest.mark.parametrize(
    ("anthology_id", "expected"),
    [
        ("2023.acl-long.1", "https://aclanthology.org/2023.acl-long.1.pdf"),
        ("p18-1001", "https://aclanthology.org/P18-1001.pdf"),
        ("W04-1013", "https://aclanthology.org/W04-1013.pdf"),
    ],
)
def test_pdf_url(anthology_id: str, expected: str) -> None:
    assert pdf_url(anthology_id) == expected


@pytest.mark.parametrize("anthology_id", ["../other", "P18-1001/other", "", "x?y"])
def test_pdf_url_rejects_invalid_id(anthology_id: str) -> None:
    with pytest.raises(ValueError, match="invalid ACL Anthology ID"):
        pdf_url(anthology_id)
