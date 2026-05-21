"""Shared fixtures for ``tests/ingest/`` — vendored from
``acatome-extract/tests/conftest.py`` and
``acatome-meta/tests/conftest.py``.

Only the fixtures actually consumed by vendored tests are carried
across; bundle-format fixtures from the acatome-extract side are
dropped because the .acatome bundle format dies in v2.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def sample_pdf_meta() -> dict:
    """Fake PDF metadata for unit tests."""
    return {
        "info": {
            "title": "Quantum Error Correction in Practice",
            "author": "Smith, John",
            "creationDate": "D:20240115120000",
        },
        "xmp": (
            '<rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/">'
            "<dc:identifier>doi:10.1038/s41567-024-1234-5</dc:identifier>"
            "</rdf:Description>"
        ),
        "doi": "10.1038/s41567-024-1234-5",
        "pdf_hash": "a" * 64,
        "first_pages_text": (
            "Quantum Error Correction in Practice\nJohn Smith\nDepartment of Physics"
        ),
        "page_count": 12,
    }


@pytest.fixture
def sample_crossref_response() -> dict:
    """Fake CrossRef API response."""
    return {
        "message": {
            "title": ["Quantum Error Correction in Practice"],
            "author": [
                {"family": "Smith", "given": "John"},
                {"family": "Jones", "given": "Alice"},
            ],
            "published-print": {"date-parts": [[2024, 1, 15]]},
            "container-title": ["Nature Physics"],
            "abstract": "We present a new approach...",
            "type": "journal-article",
            "DOI": "10.1038/s41567-024-1234-5",
        }
    }
