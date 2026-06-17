"""Tests for ``precis.workers.fetch_google_patents`` (T12.4).

Coverage shape:

* :func:`parse_google_patent_html` — pure parser. Exercise the
  three sections (abstract / description / claims) on a tiny
  synthetic HTML that mirrors patents.google.com's structure;
  no fixtures pulled from the network.
* :func:`_claim_patents_for_gp` — selection query: matches
  ``awaiting-fulltext`` + ``fulltext-unavailable``, excludes
  ``gp-attempted`` unless ``force=True``.
* :func:`run_gp_fetch_pass` — end-to-end with monkeypatched
  ``_fetch_one`` so no network is touched. Asserts blocks land,
  status tags flip, awaiting/unavailable tags drop.

The env gate is unset by default; tests that exercise the pass
toggle ``PRECIS_GP_FETCH=1`` explicitly via ``monkeypatch.setenv``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from precis.store import Store, Tag
from precis.workers import fetch_google_patents as gp
from precis.workers.fetch_google_patents import (
    GP_ATTEMPTED_TAG,
    GP_FETCHED_TAG,
    GP_NOT_FOUND_TAG,
    _claim_patents_for_gp,
    parse_google_patent_html,
    run_gp_fetch_pass,
)

# ---------------------------------------------------------------------------
# Pure-parser tests — no DB / network
# ---------------------------------------------------------------------------


_FAKE_PAGE = """
<!doctype html>
<html>
  <body>
    <section itemprop="abstract" lang="en">
      <h2>Abstract</h2>
      <div class="abstract" lang="en">
        A method for catalytic reduction of CO2 using a copper-zinc
        catalyst at moderate temperatures.
      </div>
    </section>

    <section itemprop="description" lang="en">
      <heading id="h-0001">BACKGROUND</heading>
      <div class="description-paragraph" id="p-0001" num="0001">
        Conventional CO2 reduction processes require high-pressure
        autoclaves and noble-metal catalysts.
      </div>
      <heading id="h-0002">SUMMARY</heading>
      <div class="description-paragraph" id="p-0002" num="0002">
        The present invention provides a low-pressure route using
        Earth-abundant Cu/Zn.
      </div>
    </section>

    <section itemprop="claims" lang="en">
      <div class="claims" lang="en">
        <claim id="CLM-00001" num="00001">
          <claim-text>1. A method for catalytic CO2 reduction comprising
          contacting a CO2 stream with a Cu/Zn catalyst.</claim-text>
        </claim>
        <claim id="CLM-00002" num="00002">
          <claim-text>2. The method of claim 1, wherein the catalyst
          comprises a 1:1 molar ratio of Cu to Zn.</claim-text>
        </claim>
      </div>
    </section>
  </body>
</html>
"""


def test_parse_extracts_abstract_description_and_claims() -> None:
    parsed = parse_google_patent_html(_FAKE_PAGE)
    assert parsed.abstract is not None
    assert "copper-zinc catalyst" in parsed.abstract

    # Description has 4 blocks: 2 headings (prefixed `# `) + 2 paragraphs,
    # in source order.
    assert len(parsed.description_paragraphs) == 4
    assert parsed.description_paragraphs[0] == "# BACKGROUND"
    assert "high-pressure autoclaves" in parsed.description_paragraphs[1]
    assert parsed.description_paragraphs[2] == "# SUMMARY"
    assert "Earth-abundant Cu/Zn" in parsed.description_paragraphs[3]

    # Two claims, each its own block.
    assert len(parsed.claim_texts) == 2
    assert parsed.claim_texts[0].startswith("1.")
    assert parsed.claim_texts[1].startswith("2.")


def test_parse_returns_empty_on_unrelated_html() -> None:
    parsed = parse_google_patent_html("<html><body>nothing here</body></html>")
    assert parsed.is_empty
    assert parsed.abstract is None
    assert parsed.description_paragraphs == []
    assert parsed.claim_texts == []


def test_parse_decodes_entities() -> None:
    html = (
        '<section itemprop="abstract"><div class="abstract">'
        "A &amp; B &#x2192; C"
        "</div></section>"
    )
    parsed = parse_google_patent_html(html)
    assert parsed.abstract is not None
    # Entities should decode; arrow becomes U+2192.
    assert "A & B → C" in parsed.abstract


def test_parse_falls_back_to_section_dump_when_no_paragraph_div() -> None:
    """Older HTML or oddly-shaped pages — no <div class='description-
    paragraph'>, just raw text. Parser should still get something."""
    html = (
        '<section itemprop="description">'
        "First paragraph of description.\n\n"
        "Second paragraph of description."
        "</section>"
    )
    parsed = parse_google_patent_html(html)
    assert len(parsed.description_paragraphs) == 2
    assert "First paragraph" in parsed.description_paragraphs[0]
    assert "Second paragraph" in parsed.description_paragraphs[1]


# ---------------------------------------------------------------------------
# Selection-query tests
# ---------------------------------------------------------------------------


def _seed_patent(
    store: Store,
    *,
    cite_key: str,
    status_tag: str | None,
    gp_attempted: bool = False,
    pub_date: str = "2024-01-15",
) -> int:
    """Seed a patent ref with the requested OPS-state tag."""
    ref = store.insert_ref(
        kind="patent",
        slug=cite_key,
        title=f"Patent {cite_key}",
        meta={"publication_date": pub_date},
    )
    if status_tag is not None:
        store.add_tag(ref.id, Tag.open(status_tag), set_by="test")
    if gp_attempted:
        store.add_tag(ref.id, Tag.open(GP_ATTEMPTED_TAG), set_by="test")
    return ref.id


def test_claim_picks_awaiting_and_unavailable(store: Store) -> None:
    a = _seed_patent(store, cite_key="us20240000001a1", status_tag="awaiting-fulltext")
    b = _seed_patent(store, cite_key="cn202410000002a", status_tag="fulltext-unavailable")
    # Decoy: not patent-stuck-state at all.
    _seed_patent(store, cite_key="ep1111111b1", status_tag=None)

    candidates = _claim_patents_for_gp(store, limit=10)
    found = {c.cite_key for c in candidates}
    assert "us20240000001a1" in found
    assert "cn202410000002a" in found
    assert "ep1111111b1" not in found

    by_slug = {c.cite_key: c for c in candidates}
    assert by_slug["us20240000001a1"].status_tag == "awaiting-fulltext"
    assert by_slug["cn202410000002a"].status_tag == "fulltext-unavailable"
    # Use the ref_ids so they aren't flagged unused.
    assert by_slug["us20240000001a1"].ref_id == a
    assert by_slug["cn202410000002a"].ref_id == b


def test_claim_excludes_gp_attempted_unless_force(store: Store) -> None:
    _seed_patent(
        store,
        cite_key="us20240000003a1",
        status_tag="fulltext-unavailable",
        gp_attempted=True,
    )

    # Default: skipped.
    assert _claim_patents_for_gp(store, limit=10) == []

    # force=True picks it up.
    forced = _claim_patents_for_gp(store, limit=10, force=True)
    assert any(c.cite_key == "us20240000003a1" for c in forced)


# ---------------------------------------------------------------------------
# End-to-end pass tests — monkeypatched httpx
# ---------------------------------------------------------------------------


_NOW = datetime(2026, 6, 17, tzinfo=UTC)


@pytest.fixture
def gp_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Toggle the env gate on for the test."""
    monkeypatch.setenv("PRECIS_GP_FETCH", "1")


def test_pass_skips_when_env_unset(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without PRECIS_GP_FETCH, the pass exits immediately even with
    matching candidates."""
    monkeypatch.delenv("PRECIS_GP_FETCH", raising=False)
    _seed_patent(store, cite_key="us20240000001a1", status_tag="awaiting-fulltext")

    out = run_gp_fetch_pass(store, limit=5)
    assert out == {"claimed": 0, "ok": 0, "failed": 0}


def test_pass_ingests_blocks_on_success(
    store: Store,
    gp_enabled: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful fetch lands description + claim blocks, flips meta,
    and rotates the open tags (gp-attempted + gp-fetched on,
    awaiting/unavailable off)."""
    ref_id = _seed_patent(
        store, cite_key="us20240000001a1", status_tag="awaiting-fulltext"
    )

    def _fake_fetch(slug: str) -> tuple[str, str | None, int]:
        assert slug == "us20240000001a1"
        return "ok", _FAKE_PAGE, len(_FAKE_PAGE)

    monkeypatch.setattr(gp, "_fetch_one", _fake_fetch)

    out = run_gp_fetch_pass(store, limit=5, now=_NOW)
    assert out["claimed"] == 1
    assert out["ok"] == 1
    assert out["failed"] == 0

    # Blocks landed.
    ref = store.get_ref(kind="patent", id="us20240000001a1")
    assert ref is not None
    blocks = store.list_blocks_for_ref(ref.id)
    assert len(blocks) >= 4  # at least 4 desc + 2 claim minus any merging

    # Meta got the gp_* stamps + has_description / has_claims.
    meta = ref.meta or {}
    assert meta.get("gp_status") == "fetched"
    assert meta.get("gp_blocks_added", 0) >= 1
    assert meta.get("has_description") is True
    assert meta.get("has_claims") is True
    assert "gp_source_url" in meta

    # Tags rotated.
    tag_values = {t.value for t in store.tags_for(ref_id) if t.namespace == "open"}
    assert GP_ATTEMPTED_TAG in tag_values
    assert GP_FETCHED_TAG in tag_values
    assert "awaiting-fulltext" not in tag_values
    assert "fulltext-unavailable" not in tag_values


def test_pass_handles_404_terminal(
    store: Store, gp_enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """patents.google.com 404 → gp-attempted + gp-not-found; no blocks."""
    _seed_patent(store, cite_key="zz999999a1", status_tag="awaiting-fulltext")

    def _fake_fetch(slug: str) -> tuple[str, str | None, int]:
        return "not-found", None, 0

    monkeypatch.setattr(gp, "_fetch_one", _fake_fetch)

    out = run_gp_fetch_pass(store, limit=5, now=_NOW)
    assert out["claimed"] == 1
    assert out["ok"] == 1  # 404 is a terminal outcome from the worker's POV
    assert out["failed"] == 0

    ref = store.get_ref(kind="patent", id="zz999999a1")
    assert ref is not None
    tag_values = {t.value for t in store.tags_for(ref.id) if t.namespace == "open"}
    assert GP_ATTEMPTED_TAG in tag_values
    assert GP_NOT_FOUND_TAG in tag_values
    # The patent is still missing fulltext — the awaiting tag stays.
    assert "awaiting-fulltext" in tag_values


def test_pass_http_error_is_transient(
    store: Store, gp_enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HTTP 503 / network failure shouldn't burn the patent's one
    allowed attempt — gp-attempted is NOT set, so the next pass retries."""
    _seed_patent(store, cite_key="us20240000004a1", status_tag="awaiting-fulltext")

    def _fake_fetch(slug: str) -> tuple[str, str | None, int]:
        return "http-error", "HTTP 503", 0

    monkeypatch.setattr(gp, "_fetch_one", _fake_fetch)

    out = run_gp_fetch_pass(store, limit=5, now=_NOW)
    assert out["claimed"] == 1
    assert out["failed"] == 1

    ref = store.get_ref(kind="patent", id="us20240000004a1")
    assert ref is not None
    tag_values = {t.value for t in store.tags_for(ref.id) if t.namespace == "open"}
    assert GP_ATTEMPTED_TAG not in tag_values  # transient — next pass will retry


def test_pass_parse_error_marks_attempted(
    store: Store, gp_enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Page loaded but no sections matched → gp-attempted + gp-parse-error.
    Operator can clear with --force after fixing the parser."""
    _seed_patent(store, cite_key="us20240000005a1", status_tag="awaiting-fulltext")

    def _fake_fetch(slug: str) -> tuple[str, str | None, int]:
        return "ok", "<html><body>blank</body></html>", 1000

    monkeypatch.setattr(gp, "_fetch_one", _fake_fetch)

    out = run_gp_fetch_pass(store, limit=5, now=_NOW)
    assert out["claimed"] == 1
    assert out["failed"] == 1

    ref = store.get_ref(kind="patent", id="us20240000005a1")
    assert ref is not None
    meta = ref.meta or {}
    assert meta.get("gp_status") == "parse-error"
    tag_values = {t.value for t in store.tags_for(ref.id) if t.namespace == "open"}
    assert GP_ATTEMPTED_TAG in tag_values


def test_pass_no_candidates_returns_zero(
    store: Store, gp_enabled: None
) -> None:
    """When nothing is due, the pass returns the no-op shape without
    touching the DB."""
    out = run_gp_fetch_pass(store, limit=5)
    assert out == {"claimed": 0, "ok": 0, "failed": 0}


# ---------------------------------------------------------------------------
# Drift smoke test
# ---------------------------------------------------------------------------


def test_awaiting_tag_constants_match_ingest_pipeline() -> None:
    """The worker duplicates the awaiting / unavailable tag names to
    avoid importing the ingest pipeline. Catch drift if either side
    renames."""
    from precis.handlers._patent_ingest import (
        AWAITING_FULLTEXT_TAG,
        FULLTEXT_UNAVAILABLE_TAG,
    )

    # The duplicates are private (_AWAITING_TAG / _UNAVAILABLE_TAG) so
    # we re-import via the module attribute.
    assert gp._AWAITING_TAG == AWAITING_FULLTEXT_TAG
    assert gp._UNAVAILABLE_TAG == FULLTEXT_UNAVAILABLE_TAG


