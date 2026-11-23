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


def test_job_minted_renders_handle_not_bare_id() -> None:
    """gr769707: a bare ``job_id=N`` in the todo log taught callers to
    paste N into ``get(kind='todo', id=N)`` — the summary must carry the
    ``jo<N>`` handle instead."""
    payload = {
        "job_id": 753644,
        "job_type": "taproot_backfill",
        "executor": "claude_inproc",
    }
    for source in ("minter", "dispatch", "plan_dispatch"):
        assert (
            _summarise(source, "job-minted", payload, None)
            == "jo753644 taproot_backfill executor=claude_inproc"
        )


def test_job_minted_unknown_id_falls_back_to_raw() -> None:
    payload = {"job_id": "abc", "job_type": "taproot_backfill"}
    assert (
        _summarise("minter", "job-minted", payload, None)
        == "job_id='abc' taproot_backfill"
    )


def test_job_minted_unicode_digit_id_falls_back_to_raw() -> None:
    """``'²'``.isdigit() is True but ``int('²')`` raises — the renderer
    must show the raw id, not fail the whole log read."""
    payload = {"job_id": "²", "job_type": "taproot_backfill"}
    assert (
        _summarise("minter", "job-minted", payload, None)
        == "job_id='²' taproot_backfill"
    )


def test_job_minted_summary_stays_one_line_and_bounded() -> None:
    """The job-minted arm shares the one-line, 384-character contract:
    multiline values and a large raw-id fallback must not break the
    table row."""
    payload = {
        "job_id": "x" * 500,
        "job_type": "taproot\nbackfill",
        "executor": "claude_inproc",
    }
    summary = _summarise("minter", "job-minted", payload, None)
    assert "\n" not in summary
    assert len(summary) <= 384
