"""``rate_limited`` fetcher events name their provider and limiter (gr491589)."""

from __future__ import annotations

import pytest

from precis.handlers._event_log_render import _summarise


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {"arxiv": "2401.00001", "provider": "arxiv", "limiter": "shared"},
            "arXiv rate limit hit (limiter=shared)",
        ),
        (
            {"doi": "10.1/x", "provider": "crossref", "limiter": "shared"},
            "Crossref rate limit hit (limiter=shared)",
        ),
        (
            {"doi": "10.1/x", "provider": "openalex", "limiter": "shared"},
            "OpenAlex rate limit hit (limiter=shared)",
        ),
        # Legacy Unpaywall HTTP 429: no provider, no limiter.
        ({"doi": "10.1/x", "retry_after": "60"}, "Unpaywall rate limit hit"),
    ],
)
def test_rate_limited_names_provider_and_limiter(
    payload: dict[str, str], expected: str
) -> None:
    assert _summarise("fetcher:fetch_oa", "rate_limited", payload, None) == expected


def test_rate_limited_unknown_provider_shows_raw_key() -> None:
    payload = {"provider": "zenodo", "limiter": "shared"}
    assert (
        _summarise("fetcher:fetch_oa", "rate_limited", payload, None)
        == "zenodo rate limit hit (limiter=shared)"
    )
