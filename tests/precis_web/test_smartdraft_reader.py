"""Smartdraft reader route (``GET /smartdraft/{ident}``) — the fisheye-rail
three-pane HTML surface.

The classic ``/drafts`` fixture (``tests/precis_web/test_drafts.py``'s
``DraftFakeStore``) predates the smartdraft reader: its chunk stand-in lacks
``.dc`` (the deterministic-chunk-address ``ChunkNode`` needs), so it 500s
smartdraft's ``_build_nodes_uncached`` (gripe 171217). Rather than risk that
large shared fixture, this file carries its own small ``FakeStore`` subclass
whose chunk stand-in supplies exactly the fields ``_build_nodes_uncached``
reads (``smartdraft.py`` ~lines 218-250): ``dc``, ``handle``, ``chunk_id``,
``depth``, ``chunk_kind``, ``text``, ``meta`` (``getattr``-guarded — only a
``chunk_kind='table'`` chunk needs it, for ``table_payload``).
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.store._draft_ops import DraftReviewRow
from precis.utils import handle_registry
from precis_web import smartdraft
from precis_web.app import create_app
from precis_web.config import WebConfig

from .conftest import FakeRuntime, FakeStore, make_ref

_DRAFT = make_ref(id=700, kind="draft", slug="sdt", title="Smartdraft reader draft")


def _sd_chunk(
    chunk_id: int,
    kind: str,
    text: str,
    depth: int,
    *,
    meta: dict[str, object] | None = None,
    parent_chunk_id: int | None = None,
) -> SimpleNamespace:
    handle = f"H{chunk_id:06d}"
    return SimpleNamespace(
        handle=handle,
        dc=handle_registry.format_handle("draft", chunk_id, chunk=True),
        chunk_kind=kind,
        text=text,
        depth=depth,
        chunk_id=chunk_id,
        ref_id=700,
        meta=meta or {},
        # The toc document-altitude lens's document-shape stats reuse
        # `precis.utils.wordcount.aggregate_word_counts`, which reads this
        # off every chunk (``_ChunkLike`` protocol) — default None (a flat,
        # unnested fixture) is enough for every existing test.
        parent_chunk_id=parent_chunk_id,
    )


class SmartDraftFakeStore(FakeStore):
    def __init__(self) -> None:
        super().__init__()
        self._chunks = [
            _sd_chunk(1, "heading", "Smartdraft reader draft", 0),
            _sd_chunk(
                2,
                "paragraph",
                "First body paragraph about alpha topics, see paper:acheson26.",
                1,
            ),
            _sd_chunk(3, "paragraph", "Second body paragraph about beta topics.", 1),
            # A table chunk — exercises the shared tableEditor
            # grid (gripe 56746) in the smartdraft focus pane.
            _sd_chunk(
                4,
                "table",
                "| A | B |\n| --- | --- |\n| 1 | 2 |",
                1,
                meta={"table": {"header": ["A", "B"], "rows": [["1", "2"]]}},
            ),
            # A blob-backed figure — exercises the shared
            # figure media render + clearance badge (gripe 56668) in the
            # smartdraft focus pane + the Collaborate pane's clearance list.
            _sd_chunk(
                5,
                "figure",
                "Fig 1. A diagram.",
                0,
                meta={"figure": {"origin": "original"}},
            ),
            # A registry term carrying a dedicated ``abbrev``
            # (gripe 56690) — exercises ChunkNode.is_term / term_abbrev and
            # the Collaborate-pane "occurs in N places" backlink rail.
            _sd_chunk(
                6,
                "term",
                "stereolithography",
                1,
                meta={
                    "registry": "glossary",
                    "short": "stereolithography",
                    "abbrev": "STL",
                },
            ),
            # Uses the LONG form only.
            _sd_chunk(
                7,
                "paragraph",
                "The prototype was printed via stereolithography overnight.",
                1,
            ),
            # Uses ONLY the dedicated abbrev surface — proves it resolves
            # independently of the short/long form for occurrence matching.
            _sd_chunk(8, "paragraph", "STL parts cure under UV light.", 1),
            # Mentions neither surface — must NOT show up as an occurrence.
            _sd_chunk(9, "paragraph", "Unrelated paragraph about topology.", 1),
        ]

    def get_ref(self, *, kind, id):
        if kind == "draft" and id in ("sdt", 700):
            return _DRAFT
        return super().get_ref(kind=kind, id=id)

    def list_refs(self, *, kind=None, limit=50, offset=0, **kw):
        if kind == "draft":
            return [_DRAFT]
        return super().list_refs(kind=kind, limit=limit, offset=offset, **kw)

    def reading_order(self, ref_id):
        return list(self._chunks)

    def block_views(self, ref_id, handles=None):
        # keyed by handle (matches _build_nodes_uncached's views.get(c.handle))
        return {
            "H000002": {"summary": "Alpha gist.", "keywords": "alpha"},
            "H000003": {"summary": "Beta gist.", "keywords": "beta"},
        }

    def fetch_refs_by_ids(self, ids, *, include_deleted=False):
        # draft_eyes.load_marks reads ref.meta off this — base FakeStore's
        # per-kind pools don't carry a "draft" bucket, so splice it in.
        base = super().fetch_refs_by_ids(ids, include_deleted=include_deleted)
        if 700 in ids:
            base[700] = _DRAFT
        return base

    def review_status_for_draft(self, ref_id):
        # chunk 2 reviewable but never reviewed (checker=None); chunk 3 reviewed
        # by a human, clean (dirty=False) — exercises the ✓ port's on/plain
        # states in the focus header (test below).
        return [
            DraftReviewRow(
                chunk_id=2,
                handle="H000002",
                chunk_kind="paragraph",
                section_chunk_id=1,
                checker=None,
                approved_sha=None,
                verdict=None,
                at=None,
                dirty=True,
            ),
            DraftReviewRow(
                chunk_id=3,
                handle="H000003",
                chunk_kind="paragraph",
                section_chunk_id=1,
                checker="human",
                approved_sha="abc",
                verdict="approved",
                at=None,
                dirty=False,
            ),
        ]

    def get_chunk_blob(self, handle):
        if handle == "H000005":
            return (b"\x89PNG\r\n\x1a\n", "image/png")
        return None

    def chunk_blob_version(self, chunk_id) -> str | None:
        # The fixture figure (chunk_id=5) is a real blob-backed image (the
        # medium resolver) — mirrors DraftFakeStore's FIGFIG.
        return "fixturesha0005" if chunk_id == 5 else None


@pytest.fixture
def smartdraft_runtime() -> FakeRuntime:
    return FakeRuntime(SmartDraftFakeStore())


@pytest.fixture
def smartdraft_client(smartdraft_runtime: FakeRuntime, tmp_path) -> TestClient:
    app = create_app(
        runtime=smartdraft_runtime, web_config=WebConfig(corpus_dir=tmp_path)
    )
    return TestClient(app)


class ReviewMatrixFakeStore(SmartDraftFakeStore):
    """A richer ledger than ``SmartDraftFakeStore``'s 2-row fixture — one
    chunk per review-indicator dot state, PLUS the heading
    (chunk 1) carrying its own ``structure``/``adversarial``/``toc`` rows
    (so a heading focus is reviewable at all — the base fixture's ledger
    never covers chunk 1). ``chunk_kind``/``section_chunk_id`` are set on
    every row (the base fixture's leaves them unset) so the via-section
    machine-green derivation and the toolbar's per-checker/prose-only
    rollup are both exercised:

    * chunk 1 (heading) — ``structure``+``adversarial``+``toc`` all
      current, no ``human`` → **machine**.
    * chunk 2 (paragraph, section=1) — own ``flow``+``cites`` current, AND
      chunk 1's section lenses current → **machine** (via-section).
    * chunk 3 (paragraph, section=1) — ``human`` approved, clean →
      **human**.
    * chunk 7 (paragraph, section=1) — ``human`` approved but ``dirty``
      (edited since) → **dirty**.
    * chunk 8, chunk 9 (paragraph, section=1) — reviewable, nothing ever
      run → **empty**. Rollup denominator (prose-only): chunks
      2/3/7/8/9 = 5; ``done`` (human approved clean) = chunk 3 only → 1/5.
    * chunk 4 (table, section=1) — reviewable, nothing ever run → **empty**,
      but NOT prose or heading, so item 3's dropdown gate offers it NO
      run-lens buttons (excluded from the prose-only rollup denominator
      too — the ``1/5`` above stays exactly 5, not 6).
    """

    def review_status_for_draft(self, ref_id):
        def row(
            chunk_id, checker, *, dirty, kind="paragraph", section=1, verdict="approved"
        ):
            return DraftReviewRow(
                chunk_id=chunk_id,
                handle=f"H{chunk_id:06d}",
                chunk_kind=kind,
                section_chunk_id=section,
                checker=checker,
                approved_sha="old" if dirty else "cur",
                verdict=verdict,
                at=None,
                dirty=dirty,
            )

        return [
            row(
                1, "structure", dirty=False, kind="heading", section=None, verdict="ok"
            ),
            row(
                1,
                "adversarial",
                dirty=False,
                kind="heading",
                section=None,
                verdict="ok",
            ),
            row(1, "toc", dirty=False, kind="heading", section=None, verdict="ok"),
            row(2, "flow", dirty=False, verdict="ok"),
            row(2, "cites", dirty=False, verdict="ok"),
            row(3, "human", dirty=False),
            row(4, None, dirty=True, kind="table"),
            row(7, "human", dirty=True),
            row(8, None, dirty=True),
            row(9, None, dirty=True),
        ]


@pytest.fixture
def review_matrix_runtime() -> FakeRuntime:
    return FakeRuntime(ReviewMatrixFakeStore())


@pytest.fixture
def review_matrix_client(review_matrix_runtime: FakeRuntime, tmp_path) -> TestClient:
    app = create_app(
        runtime=review_matrix_runtime, web_config=WebConfig(corpus_dir=tmp_path)
    )
    return TestClient(app)


class BigDraftFakeStore(SmartDraftFakeStore):
    """A heading + 100 body paragraphs — enough to exceed the full-document
    render window (``smartdraft._FULLDOC_WINDOW`` = 40) so distant chunks become
    lazy ``data-ph`` placeholders."""

    def __init__(self) -> None:
        super().__init__()
        # Each paragraph is long (> the 140-char TOC-summary cap) with a unique
        # END-marker at the tail: the marker survives ONLY in a fully-rendered
        # middle block, never in the truncated TOC summary — the virtualization
        # probe (a placeholder chunk ships neither its body nor its marker).
        filler = "lorem ipsum dolor sit amet " * 8
        self._chunks = [_sd_chunk(1, "heading", "Big draft", 0)] + [
            _sd_chunk(i, "paragraph", f"Para {i}. {filler} tail{i}end.", 1)
            for i in range(2, 102)
        ]

    def review_status_for_draft(self, ref_id):
        return []


@pytest.fixture
def big_draft_client(tmp_path) -> TestClient:
    app = create_app(
        runtime=FakeRuntime(BigDraftFakeStore()),
        web_config=WebConfig(corpus_dir=tmp_path),
    )
    return TestClient(app)


class CitedBigDraftFakeStore(BigDraftFakeStore):
    """Like ``BigDraftFakeStore``, but every paragraph carries a bracket-
    handle paper citation (``[pc9999]``) AND every one of the 101 chunks
    (including the ~60 that fall outside the ±40 full-doc render window
    and become ``skel`` placeholders) is reviewable — item 1's regression
    fixture: before the fix, ``review_payloads_for`` ran the citation-
    integrity check (``resolve_handle``) over every node in ``view.middle``,
    including the inert skel spacers that never reach the template."""

    def __init__(self) -> None:
        super().__init__()
        filler = "lorem ipsum dolor sit amet " * 8
        self._chunks = [_sd_chunk(1, "heading", "Big draft", 0)] + [
            _sd_chunk(
                i, "paragraph", f"Para {i} cites [pc9999]. {filler} tail{i}end.", 1
            )
            for i in range(2, 102)
        ]

    def review_status_for_draft(self, ref_id):
        return [
            DraftReviewRow(
                chunk_id=c.chunk_id,
                handle=c.handle,
                chunk_kind=c.chunk_kind,
                section_chunk_id=None if c.chunk_kind == "heading" else 1,
                checker=None,
                approved_sha=None,
                verdict=None,
                at=None,
                dirty=True,
            )
            for c in self._chunks
        ]


def test_smartdraft_full_doc_review_payload_skips_skel_placeholders(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """item 1: full-document (📄) mode's review-payload build must scope the
    citation-integrity check (``cite_integrity_ok``, which calls
    ``store.resolve_handle``/``store.count_blocks`` per cited paper) to what
    actually RENDERS (the ``full``/``doc``/``tail``/``head`` middle rows),
    not the whole ``view.middle`` list — the ``skel`` placeholder rows
    (inert scroll spacers, ~60 of the 101 chunks here) never reach
    ``sd_review_widget`` and must not cost ``cite_integrity_ok`` a call each.
    Counts calls to ``smartdraft.cite_integrity_ok`` itself (not the raw
    store hit) — the page ALSO runs an unrelated citation scan
    (``_hub_and_citation_stats`` → ``_collect_raw_cites``, item 5(a)'s
    rollup numbers, itself scoped to the rendered window post the
    "/smartdraft reader" perf fix) that would otherwise swamp a bare
    ``resolve_handle`` call count."""
    store = CitedBigDraftFakeStore()
    calls: list[str] = []
    from precis_web import smartdraft as smartdraft_mod

    orig = smartdraft_mod.cite_integrity_ok

    def counting(store_arg, text, cache):
        calls.append(text)
        return orig(store_arg, text, cache)

    monkeypatch.setattr(smartdraft_mod, "cite_integrity_ok", counting)
    app = create_app(
        runtime=FakeRuntime(store), web_config=WebConfig(corpus_dir=tmp_path)
    )
    client = TestClient(app)
    r = client.get("/smartdraft/sdt?relevance=0&focus=dc1")
    assert r.status_code == 200
    # The ±40 full-doc window around the focus (chunk 1, idx 0) renders 41
    # nodes (idx 0..40) — every one of those is reviewable, so the integrity
    # check runs once per rendered node. The other ~60 chunks (idx 41..100,
    # reviewable but never rendered — ``skel`` placeholders) must trigger
    # NONE at all.
    assert len(calls) == 41


# ── claim_trust_for_block — the unit-level counterpart to cite_integrity_ok
# (the trust-surfaces editor badges). ``claim_trust`` itself is monkeypatched — its
# real derivation over hub/lifecycle state is ``test_taproot_trust.py``'s job,
# DB-backed; this only proves ``claim_trust_for_block``'s head-scan, cache,
# and worst-of/ignore-unresolved contract. ``store`` is never dereferenced by
# the FI-handle path (a pure ``handle_registry.parse``), so a bare ``object()``
# stands in.


def test_claim_trust_for_block_unverified_head(monkeypatch: pytest.MonkeyPatch) -> None:
    from precis.taproot.trust import TrustState

    monkeypatch.setattr(
        "precis.taproot.trust.claim_trust",
        lambda store, ref_id: TrustState(
            label="unverified",
            note="source pending",
            overridden=False,
            status="tracing",
        ),
    )

    result = smartdraft.claim_trust_for_block(object(), "See [fi42] for details.", {})

    assert result == {
        "label": "unverified",
        "heads": [{"head": "fi42", "label": "unverified", "note": "source pending"}],
    }


def test_claim_trust_for_block_unsupported_ranks_above_unverified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from precis.taproot.trust import TrustState

    states = {
        42: TrustState(
            label="unverified",
            note="source pending",
            overridden=False,
            status="tracing",
        ),
        43: TrustState(
            label="unsupported",
            note="chunk reports the opposite trend",
            overridden=False,
            status="established",
        ),
    }
    monkeypatch.setattr(
        "precis.taproot.trust.claim_trust", lambda store, ref_id: states[ref_id]
    )

    result = smartdraft.claim_trust_for_block(object(), "See [fi42] and [fi43].", {})

    assert result is not None
    assert result["label"] == "unsupported"
    assert {h["head"] for h in result["heads"]} == {"fi42", "fi43"}


def test_claim_trust_for_block_all_clean_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from precis.taproot.trust import TrustState

    monkeypatch.setattr(
        "precis.taproot.trust.claim_trust",
        lambda store, ref_id: TrustState(
            label="clean", note=None, overridden=False, status="established"
        ),
    )

    assert smartdraft.claim_trust_for_block(object(), "Clean claim [fi42].", {}) is None


def test_claim_trust_for_block_no_heads_skips_scan_entirely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No bracket token at all → the cheap regex pre-check skips the head
    scan (and therefore ``claim_trust``) entirely — never even reaches the
    cite-head grammar."""
    calls: list[int] = []
    monkeypatch.setattr(
        "precis.taproot.trust.claim_trust",
        lambda store, ref_id: calls.append(ref_id),
    )

    result = smartdraft.claim_trust_for_block(object(), "Plain prose, no cites.", {})

    assert result is None
    assert calls == []


def test_claim_trust_for_block_ignores_unresolved_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cite-head-shaped token that doesn't resolve to a finding (a bare
    paper cite, or prose that merely looks like a head) is ignored — that's
    ``cite_integrity_ok``'s domain, not trust's."""
    monkeypatch.setattr(
        "precis_web.claim_render.resolve_head_ref_id", lambda store, head: None
    )

    result = smartdraft.claim_trust_for_block(object(), "See [abcdef] over there.", {})

    assert result is None


def test_claim_trust_for_block_shares_cache_across_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One ``claim_trust`` store round-trip per distinct head across the
    WHOLE render, not per block — the shared ``cache`` dict, exactly like
    ``cite_integrity_ok``'s own (docstring's stated per-render cost bar)."""
    from precis.taproot.trust import TrustState

    calls: list[int] = []

    def fake_claim_trust(store: object, ref_id: int) -> TrustState:
        calls.append(ref_id)
        return TrustState(
            label="unverified",
            note="source pending",
            overridden=False,
            status="tracing",
        )

    monkeypatch.setattr("precis.taproot.trust.claim_trust", fake_claim_trust)
    cache: dict[str, object] = {}

    smartdraft.claim_trust_for_block(object(), "See [fi42].", cache)
    smartdraft.claim_trust_for_block(object(), "Also cites [fi42] again.", cache)

    assert calls == [42]


def test_smartdraft_full_doc_virtualizes_long_draft(
    big_draft_client: TestClient,
) -> None:
    """Full-document (📄) mode renders only a window of real blocks around the
    focus; distant chunks are lazy ``data-ph`` spacers (hydrated on scroll via
    /blocks) — so a long draft's initial page is O(window), not O(N)."""
    r = big_draft_client.get("/smartdraft/sdt?relevance=0&focus=dc1")
    assert r.status_code == 200
    body = r.text
    # a distant chunk (dc90) is a placeholder — its body text was NOT shipped
    assert '<div data-dc="dc90" data-ph' in body
    assert "tail90end" not in body
    # a near chunk (dc5) IS a real middle block with its full body text
    assert "tail5end" in body
    # many placeholders exist (not everything rendered server-side)
    assert body.count("data-ph") > 30


def test_smartdraft_blocks_endpoint_hydrates_requested_dcs(
    big_draft_client: TestClient,
) -> None:
    """GET /smartdraft/{ident}/blocks?dcs=… returns the real reading blocks for
    a window of placeholder handles — the lazy-hydrate fetch. Same block markup
    the initial render uses (a ``<div data-dc>``, not a placeholder)."""
    r = big_draft_client.get("/smartdraft/sdt/blocks?dcs=dc90,dc91")
    assert r.status_code == 200
    body = r.text
    assert '<div data-dc="dc90"' in body and "data-ph" not in body
    assert "tail90end" in body
    assert "tail91end" in body
    # a handle not in the draft is silently skipped (no crash, no block)
    r2 = big_draft_client.get("/smartdraft/sdt/blocks?dcs=dc9999")
    assert r2.status_code == 200
    assert 'data-dc="dc9999"' not in r2.text


def test_smartdraft_reader_renders_three_panes(
    smartdraft_client: TestClient,
) -> None:
    """The reader 200s and mounts all three panes: left fisheye TOC (rows
    carry ``data-dc``), middle focus (``#mid-focus``), right collaborate
    (the "Collaborate" pane header)."""
    r = smartdraft_client.get("/smartdraft/sdt")
    assert r.status_code == 200
    body = r.text
    assert 'id="sd-content"' in body  # the 3-pane grid mounted at all
    assert 'data-dc="' in body  # left pane: TOC rows keyed by dc
    assert 'id="mid-focus"' in body  # middle pane: the rendered focus chunk
    assert "Collaborate" in body  # right pane header


def test_smartdraft_ask_uses_structured_llm_selector(
    smartdraft_client: TestClient,
) -> None:
    """The "Ask the LLM" picker is the shared structured selector
    (_llm_selector.html.j2): four controls — tier × placement × reasoning ×
    temperature — bound to its own Alpine scope and fetching the live
    preview off ``GET /api/llm/resolve``, NOT the old 8-alias
    ``planner_models()`` dropdown (opus/sonnet/haiku/local + the four
    canonical tiers)."""
    r = smartdraft_client.get("/smartdraft/sdt")
    assert r.status_code == 200
    body = r.text
    assert 'x-data="llmSelectorState(' in body
    assert 'x-model="tier"' in body
    assert 'x-model="placement"' in body
    assert 'x-model="reasoning"' in body
    assert 'x-model="temperature"' in body
    assert "/api/llm/resolve" in body
    # the four canonical tier options are present …
    assert '<option value="small">small</option>' in body
    assert '<option value="frontier">frontier</option>' in body
    # … but the retired 8-alias planner_models() dropdown is gone (no
    # opus/sonnet/haiku option values anywhere on the page — "local" is
    # skipped here since it's also a legitimate placement-select value).
    for legacy in ("opus", "sonnet", "haiku"):
        assert f'<option value="{legacy}"' not in body


def test_smartdraft_full_doc_cited_block_is_div_not_nested_anchor(
    smartdraft_client: TestClient,
) -> None:
    """Regression (the "[pc…] starts a new paragraph" bug): in full-document
    mode a body paragraph that cites a source (chunk 2 → ``paper:acheson26``,
    which linkifies to a ``§`` ``<a>`` anchor) must render inside a
    ``<div data-dc>`` block, NOT a block-level ``<a>``. An ``<a>`` may not wrap
    another ``<a>`` — the HTML parser runs the adoption-agency algorithm and
    auto-closes the outer block anchor at the first citation, spilling the rest
    of the paragraph onto a new visual line. The focus-nav click on the whole
    block is preserved by the ``data-dc`` delegated handler instead of an href.
    """
    # Full-document mode, focused on the heading (dc1) so the citing body para
    # (chunk 2) is a NON-focus "doc" neighbour — the block that used to break.
    dc1 = handle_registry.format_handle("draft", 1, chunk=True)
    dc2 = handle_registry.format_handle("draft", 2, chunk=True)
    r = smartdraft_client.get(f"/smartdraft/sdt?relevance=0&focus={dc1}")
    assert r.status_code == 200
    body = r.text
    # the cited neighbour block renders as a <div data-dc> (focus-nav preserved
    # via the delegated data-dc click handler, not an href) …
    open_tag = f'<div data-dc="{dc2}"'
    assert open_tag in body
    seg = body[body.index(open_tag) : body.index("</div>", body.index(open_tag))]
    # … the citation IS linkified into its own hover-preview <a> inside the
    # block (so the nesting the fix avoids was genuinely in play) …
    assert "data-popid=" in seg and "<a " in seg
    # … and the offending wrapper — a block-level <a> around wrap-preserving
    # body text (which the parser would auto-close at the first inner <a>) — is
    # gone (the neighbour <div>s use "block cursor-pointer …"; the focus <div>
    # uses "whitespace-pre-wrap" without a leading "block").
    assert 'class="block whitespace-pre-wrap' not in body


def test_smartdraft_block_review_indicator_reflects_ledger_state(
    smartdraft_client: TestClient,
) -> None:
    """Per-block review indicator (item 6): grey/"empty" for a reviewable-
    but-never-reviewed block (chunk 2), green/"human" for a clean human-
    approved block (chunk 3) — same widget on the focus regardless of which
    block it is (item 6 is "every rendered block", not just focus). The
    un-review + diff-since-approval actions appear only once a human row
    exists (chunk 3), never for a never-reviewed block (chunk 2)."""
    dc2 = handle_registry.format_handle("draft", 2, chunk=True)
    dc3 = handle_registry.format_handle("draft", 3, chunk=True)
    r2 = smartdraft_client.get(f"/smartdraft/sdt?focus={dc2}")
    assert r2.status_code == 200
    assert 'class="sd-review sd-review-dot empty"' in r2.text
    assert f"sdReviewHuman('{dc2}')" in r2.text  # dropdown's mark-reviewed action
    # dc2 (never reviewed) has no un-review/diff-since-approval action; dc3
    # (elsewhere on the same full-document page) does — scope each
    # assertion to its OWN dc so dc3's controls elsewhere don't false-positive.
    assert f"sdReviewRetract('{dc2}')" not in r2.text
    assert f"id%3D{dc2}%20view%3Dreview-diff" not in r2.text
    assert f"sdReviewRetract('{dc3}')" in r2.text
    assert f"id%3D{dc3}%20view%3Dreview-diff" in r2.text
    r3 = smartdraft_client.get(f"/smartdraft/sdt?focus={dc3}")
    assert 'class="sd-review sd-review-dot human"' in r3.text
    assert f"sdReviewRetract('{dc3}')" in r3.text
    assert f"id%3D{dc3}%20view%3Dreview-diff" in r3.text


def test_smartdraft_review_dropdown_uses_ledger_lens_vocabulary(
    review_matrix_client: TestClient,
) -> None:
    """The per-block dropdown (item 7) runs the FOUR ledger lens names —
    never the retired heading-only review▾ menu's structural/deep_review
    vocabulary — and a heading's "all" implicitly covers its subtree (the
    fanout's own scope rule), not a separate subtree control."""
    dc1 = handle_registry.format_handle("draft", 1, chunk=True)  # heading
    dc2 = handle_registry.format_handle("draft", 2, chunk=True)  # body para
    rh = review_matrix_client.get(f"/smartdraft/sdt?focus={dc1}")
    assert rh.status_code == 200
    assert "structural" not in rh.text
    assert "deep_review" not in rh.text
    assert f"sdReviewRun('{dc1}', 'structure')" in rh.text
    assert f"sdReviewRun('{dc1}', 'adversarial')" in rh.text
    assert "all (subtree)" in rh.text  # a heading's "all" covers its subtree
    rp = review_matrix_client.get(f"/smartdraft/sdt?focus={dc2}")
    assert f"sdReviewRun('{dc2}', 'flow')" in rp.text
    assert f"sdReviewRun('{dc2}', 'cites')" in rp.text
    assert "structural" not in rp.text
    assert "deep_review" not in rp.text


def test_smartdraft_review_dropdown_ineligible_kind_has_no_run_buttons(
    review_matrix_client: TestClient,
) -> None:
    """item 3: a chunk kind that is neither prose nor a heading (here,
    ``chunk_kind='table'``, dc4) gets NO run-lens buttons at all — offering
    ``flow``/``cites`` (or ``structure``/``adversarial``) on it would
    silently no-op the click, since ``review_fanout._lenses_for_kind``
    mints nothing for that kind. The human ✓ mark-reviewed entry is still
    offered (human sign-off is available on any reviewable block, by
    design) — only the machine-lens triggers are gated."""
    dc4 = handle_registry.format_handle("draft", 4, chunk=True)  # table chunk
    r = review_matrix_client.get(f"/smartdraft/sdt?focus={dc4}")
    assert r.status_code == 200
    assert f"sdReviewHuman('{dc4}')" in r.text  # human mark-reviewed still offered
    assert f"sdReviewRun('{dc4}', 'flow')" not in r.text
    assert f"sdReviewRun('{dc4}', 'cites')" not in r.text
    assert f"sdReviewRun('{dc4}', 'structure')" not in r.text
    assert f"sdReviewRun('{dc4}', 'adversarial')" not in r.text
    assert f"sdReviewRun('{dc4}', 'all')" not in r.text


def test_smartdraft_indicator_machine_green_derives_via_section(
    review_matrix_client: TestClient,
) -> None:
    """A prose block's machine state is its own ``flow``/``cites`` PLUS its
    enclosing heading's ``structure``/``adversarial`` (item 2/6's "via
    section" derivation) — chunk 2's own lenses AND chunk 1's (its section)
    are all current, with no ``human`` row, so it reads "machine" (hollow/
    blue), not "empty". The tooltip lists all four machine checkers plus
    ``human``, and section lenses are labelled "via section" so the
    tooltip never implies the paragraph itself carries them."""
    dc2 = handle_registry.format_handle("draft", 2, chunk=True)
    r = review_matrix_client.get(f"/smartdraft/sdt?focus={dc2}")
    assert r.status_code == 200
    assert 'class="sd-review sd-review-dot machine"' in r.text
    seg = r.text[r.text.index('sd-review-dot machine"') :]
    tooltip = seg[seg.index('title="') : seg.index('"', seg.index('title="') + 7)]
    assert "✓ flow" in tooltip and "✓ cites" in tooltip
    assert "✓ structure (via section)" in tooltip
    assert "✓ adversarial (via section)" in tooltip
    assert "– human" in tooltip  # never reviewed by a human yet


def test_smartdraft_indicator_dirty_amber_when_edited_since_human_approval(
    review_matrix_client: TestClient,
) -> None:
    """chunk 7 was human-approved once but has since been edited (``dirty``
    on the ``human`` row) — the indicator reads "dirty" (amber), the
    un-review action still offers (a human row exists to retract), and the
    tooltip marks the stale human row with ``⚠``."""
    dc7 = handle_registry.format_handle("draft", 7, chunk=True)
    r = review_matrix_client.get(f"/smartdraft/sdt?focus={dc7}")
    assert r.status_code == 200
    assert 'class="sd-review sd-review-dot dirty"' in r.text
    assert "sdReviewRetract(" in r.text
    assert "⚠ human" in r.text


def test_smartdraft_blocks_hydration_carries_review_payload(
    review_matrix_client: TestClient,
) -> None:
    """/blocks hydration must carry the SAME per-chunk review payload as the
    initial render (item 4) — a scrolled-to block's indicator shouldn't stay
    blank just because it hydrated after the fact."""
    dc3 = handle_registry.format_handle("draft", 3, chunk=True)  # human, clean
    r = review_matrix_client.get(f"/smartdraft/sdt/blocks?dcs={dc3}")
    assert r.status_code == 200
    assert 'class="sd-review sd-review-dot human"' in r.text


class ClaimTrustFakeStore(SmartDraftFakeStore):
    """Chunks whose prose cites findings by ``fi<id>`` handle — the
    claim-trust badge's cite-head grammar (the trust-surfaces editor
    badges). ``fi<id>`` resolves to its ref_id
    via a pure ``handle_registry.parse`` (no store hit), so these fixture
    "findings" need no backing ref row — only ``precis.taproot.trust.
    claim_trust`` itself is monkeypatched per test (deriving a real trust
    label needs ``tags_for``/``fetch_refs_by_ids``/hub machinery a plain
    ``FakeStore`` doesn't cheaply fake, mirrored by ``tests/
    test_taproot_trust.py``'s own DB-backed ``store`` fixture instead).
    Every body chunk is reviewable-but-never-reviewed ("empty" dot state)
    so the overlay is the only thing distinguishing one from another."""

    def __init__(self) -> None:
        super().__init__()
        self._chunks = [
            _sd_chunk(1, "heading", "Claim trust badge draft", 0),
            _sd_chunk(2, "paragraph", "Alpha claim [fi777].", 1),
            _sd_chunk(3, "paragraph", "Beta claim [fi778].", 1),
            _sd_chunk(4, "paragraph", "Gamma claim [fi777] and [fi778].", 1),
            _sd_chunk(5, "paragraph", "Clean claim [fi779].", 1),
        ]

    def review_status_for_draft(self, ref_id):
        return [
            DraftReviewRow(
                chunk_id=c.chunk_id,
                handle=c.handle,
                chunk_kind=c.chunk_kind,
                section_chunk_id=1,
                checker=None,
                approved_sha=None,
                verdict=None,
                at=None,
                dirty=True,
            )
            for c in self._chunks
            if c.chunk_kind != "heading"
        ]


@pytest.fixture
def claim_trust_client(tmp_path) -> TestClient:
    app = create_app(
        runtime=FakeRuntime(ClaimTrustFakeStore()),
        web_config=WebConfig(corpus_dir=tmp_path),
    )
    return TestClient(app)


def _dc_block(html: str, dc: str) -> str:
    """The single rendered ``<div data-dc="dc">…</div>`` for one block —
    ``sd_doc_block`` deliberately nests no ``<div>`` inside its widget, so
    the next ``</div>`` after the opening tag is always this block's own
    close (see ``_block.html.j2``'s own comment to that effect)."""
    marker = f'<div data-dc="{dc}"'
    start = html.index(marker)
    end = html.index("</div>", start)
    return html[start:end]


def test_smartdraft_badge_marks_unverified_and_unsupported_claims(
    monkeypatch: pytest.MonkeyPatch, claim_trust_client: TestClient
) -> None:
    """Trust-surfaces editor badges: a block citing an unverified-backed
    finding gets the amber "?" overlay class (``sd-trust-unverified``) plus
    a tooltip line naming the head; one citing an unsupported-backed
    finding gets the louder ``sd-trust-unsupported`` class + tooltip line.
    A block citing BOTH ranks worst-of: the overlay class is
    ``sd-trust-unsupported`` (not also ``-unverified``), but the tooltip
    still lists every offending head. A clean-backed citation carries
    neither class nor tooltip line (AC 6 spirit — reads like today)."""
    from precis.taproot.trust import TrustState

    states = {
        777: TrustState(
            label="unverified",
            note="source pending",
            overridden=False,
            status="tracing",
        ),
        778: TrustState(
            label="unsupported",
            note="chunk reports the opposite trend",
            overridden=False,
            status="established",
        ),
        779: TrustState(
            label="clean", note=None, overridden=False, status="established"
        ),
    }
    monkeypatch.setattr(
        "precis.taproot.trust.claim_trust", lambda store, ref_id: states[ref_id]
    )

    r = claim_trust_client.get("/smartdraft/sdt?relevance=0&focus=dc1")
    assert r.status_code == 200

    dc2 = handle_registry.format_handle("draft", 2, chunk=True)
    dc3 = handle_registry.format_handle("draft", 3, chunk=True)
    dc4 = handle_registry.format_handle("draft", 4, chunk=True)
    dc5 = handle_registry.format_handle("draft", 5, chunk=True)

    seg2 = _dc_block(r.text, dc2)
    assert "sd-trust-unverified" in seg2
    assert "sd-trust-unsupported" not in seg2
    assert "⚠ unverified claim: [fi777] — source pending" in seg2

    seg3 = _dc_block(r.text, dc3)
    assert "sd-trust-unsupported" in seg3
    assert "‼ UNSUPPORTED: [fi778] — cited source does not back this claim" in seg3

    seg4 = _dc_block(r.text, dc4)
    assert "sd-trust-unsupported" in seg4
    assert "sd-trust-unverified" not in seg4  # worst-of: unsupported wins the class
    assert "⚠ unverified claim: [fi777] — source pending" in seg4  # both heads noted
    assert "‼ UNSUPPORTED: [fi778] — cited source does not back this claim" in seg4

    seg5 = _dc_block(r.text, dc5)
    assert "sd-trust-unverified" not in seg5
    assert "sd-trust-unsupported" not in seg5


def test_smartdraft_toolbar_rollup_badge_shows_counts_and_review_complete(
    review_matrix_client: TestClient,
) -> None:
    """Toolbar badge (item 8): ``N/M`` over PROSE chunks only (2/3/7/8/9 = 5;
    only chunk 3 is human-approved-clean → ``1/5``), not yet review-complete."""
    r = review_matrix_client.get("/smartdraft/sdt")
    assert r.status_code == 200
    assert ">1/5<" in r.text
    assert (
        "complete"
        not in r.text[
            r.text.index("sd-badge-rollup") : r.text.index("sd-badge-rollup") + 60
        ]
    )


class AllHumanReviewedFakeStore(SmartDraftFakeStore):
    """Every prose chunk human-approved at its current sha — the toolbar
    badge's "review-complete" state (N == M > 0)."""

    def review_status_for_draft(self, ref_id):
        return [
            DraftReviewRow(
                chunk_id=cid,
                handle=f"H{cid:06d}",
                chunk_kind="paragraph",
                section_chunk_id=1,
                checker="human",
                approved_sha="cur",
                verdict="approved",
                at=None,
                dirty=False,
            )
            for cid in (2, 3, 7, 8, 9)
        ]


def test_smartdraft_toolbar_badge_reads_review_complete(tmp_path) -> None:
    app = create_app(
        runtime=FakeRuntime(AllHumanReviewedFakeStore()),
        web_config=WebConfig(corpus_dir=tmp_path),
    )
    client = TestClient(app)
    r = client.get("/smartdraft/sdt")
    assert r.status_code == 200
    assert ">5/5 ✓<" in r.text
    assert (
        'class="sd-badge-rollup rounded border px-1.5 py-0.5 font-semibold complete"'
        in r.text
    )


def test_smartdraft_review_widget_never_speaks_old_reviewer_vocabulary(
    review_matrix_client: TestClient,
) -> None:
    """Acceptance criterion: the smartdraft page never emits the retired
    ``structural``/``deep_review`` reviewer names anywhere — the whole
    per-block dropdown + toolbar rollup speak only the four ledger lenses
    (+ ``human``/``toc``)."""
    r = review_matrix_client.get("/smartdraft/sdt")
    assert r.status_code == 200
    assert "structural" not in r.text
    assert "deep_review" not in r.text


def test_smartdraft_focus_shows_connections_links_and_flags(
    smartdraft_client: TestClient,
    smartdraft_runtime: FakeRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The focus Connections rail (gripe 178766, migrated from the retired
    classic reader): the focus chunk's out/in link-edges (`store.drafts.chunk_connections`,
    split by direction) and its anchored change-request flags (`store.drafts.anchored_todos`)
    render as hover chips. Both surfaces are computed for the focus by
    `draft_links.chunk_links` → `_connection_chips`/`_flag_chips`."""
    store = smartdraft_runtime.store

    def fake_conns(ref_id, handles):
        return {
            "H000002": [  # chunk 2's base58 (the default focus)
                {
                    "relation": "derived-from",
                    "direction": "out",
                    "kind": "memory",
                    "ident": "20",
                    "title": "A decision",
                },
                {
                    "relation": "cites",
                    "direction": "in",
                    "kind": "memory",
                    "ident": "21",
                    "title": "A citing dream",
                },
            ]
        }

    def fake_flags(handles):
        return {
            "H000002": [{"ref_id": 99, "title": "tighten this claim", "status": "open"}]
        }

    monkeypatch.setattr(store, "chunk_connections", fake_conns)
    monkeypatch.setattr(store, "anchored_todos", fake_flags)
    dc2 = handle_registry.format_handle("draft", 2, chunk=True)
    r = smartdraft_client.get(f"/smartdraft/sdt?focus={dc2}")
    assert r.status_code == 200
    body = r.text
    assert "/r/memory/20" in body and "A decision" in body  # out edge
    assert "/r/memory/21" in body and "A citing dream" in body  # in edge
    assert "tighten this claim" in body and "/r/todo/99" in body  # anchored flag


def test_smartdraft_focus_accepts_base58_handle(
    smartdraft_client: TestClient,
) -> None:
    """The app-wide ``/c/<handle>`` + agentlog deep links focus by the legacy
    base58 anchor (``chunks.handle``), not the ``dc<id>`` form. ``focus_index``
    accepts either, so ``?focus=<base58>`` lands on the same chunk as
    ``?focus=dc<id>`` — otherwise every ¶/§ citation click would degrade to the
    first body chunk."""
    dc2 = handle_registry.format_handle("draft", 2, chunk=True)  # "dc2"
    # H000002 is chunk 2's base58 (SmartDraftFakeStore._sd_chunk).
    r = smartdraft_client.get("/smartdraft/sdt?focus=H000002")
    assert r.status_code == 200
    # the resolved focus is chunk 2 — its dc drives the hidden focus field.
    assert f'name="focus" value="{dc2}"' in r.text


def test_smartdraft_reader_loads_katex_for_inline_math(
    smartdraft_client: TestClient,
) -> None:
    """The reader loads KaTeX (CSS + katex.min.js + auto-render) and wires the
    render into ``afterSwap``/``__sdRenderMath``, so inline LaTeX like ``$sp^3$``
    renders — parity with the classic /drafts reader (detail.html.j2)."""
    r = smartdraft_client.get("/smartdraft/sdt")
    assert r.status_code == 200
    body = r.text
    assert "katex.min.css" in body  # stylesheet loaded
    assert "katex.min.js" in body  # engine loaded
    assert "auto-render.min.js" in body  # $…$ / $$…$$ scanner loaded
    assert "renderMathInElement" in body  # render helper defined
    # the initial pass + every no-reload nav swap re-render the panel
    assert "window.__sdRenderMath" in body


def test_smartdraft_reader_has_docked_claim_pane(smartdraft_client: TestClient) -> None:
    """The claim-UX docked pane: a docked "Claim" panel at the top
    of the right rail — hidden by default, with a close button and an
    "open full page" link out to ``/claim/<head>``. It's what a prose ◆ /
    Claims-rail chip click (item 5/6's delegated handler) loads
    ``/preview/claim/<head>`` into via htmx."""
    r = smartdraft_client.get("/smartdraft/sdt")
    assert r.status_code == 200
    body = r.text
    assert 'id="claim-pane"' in body
    assert (
        "hidden"
        in body[body.index('id="claim-pane"') : body.index('id="claim-pane"') + 200]
    )
    assert 'id="claim-pane-body"' in body
    assert 'id="claim-pane-close"' in body
    assert 'id="claim-pane-open"' in body
    assert "open full page ↗" in body


def test_smartdraft_reader_claim_delegate_script_present(
    smartdraft_client: TestClient,
) -> None:
    """The diamond↔rail sync + docked-pane click delegate (items 5/6): one
    delegated handler keyed on ``data-claim-head`` (shared by the prose ◆
    and its Claims-rail chip), loading the preview fragment via htmx and
    closing on the pane's own ✕ — not a listener per anchor."""
    r = smartdraft_client.get("/smartdraft/sdt")
    assert r.status_code == 200
    body = r.text
    assert "function claimPaneOpen(head)" in body
    assert "function claimPaneClose(" in body
    assert "function claimFlashCounterpart(head, from)" in body
    assert "'/preview/claim/' + head" in body
    assert "htmx.ajax(" in body
    assert "[data-claim-head]" in body
    assert "claim-pane-close" in body
    # hover sync: both diamond and rail chip toggle the same highlight class
    assert "sd-claim-hl" in body
    assert "mouseover" in body and "mouseout" in body


def test_smartdraft_claim_click_reuses_popped_out_window(
    smartdraft_client: TestClient,
) -> None:
    """Conditional claim target (Reto 2026-08-06): a ◆/rail-chip click
    retargets a *popped-out* claim window if one is open (retained handle,
    named 'precis-claim'), else falls back to the in-page docked pane. The
    pane's "open full page ↗" is what graduates the pane to that window."""
    r = smartdraft_client.get("/smartdraft/sdt")
    assert r.status_code == 200
    body = r.text
    # A retained handle for the popped-out window, opened under a stable name.
    assert "sdClaimWin" in body
    assert "'precis-claim'" in body
    # The conditional: retarget the open window, else the docked pane.
    assert "sdClaimWin && !sdClaimWin.closed" in body
    assert "claimPaneOpen(head)" in body  # the fallback branch survives


def test_smartdraft_claims_rail_chip_carries_data_claim_head(
    smartdraft_client: TestClient,
) -> None:
    """The Claims-rail chip (violet, linking to ``/claim/<head>``) carries
    the SAME ``data-claim-head`` attribute the prose ◆ anchor does (linkify
    ``_render_claim_hub``), so the hover-sync / click delegate can find both
    sides of a citation without a kind-specific lookup."""
    from pathlib import Path

    import precis_web

    tpl_path = (
        Path(precis_web.__file__).parent / "templates" / "smartdraft" / "view.html.j2"
    )
    assert 'data-claim-head="{{ entry.head }}"' in tpl_path.read_text(encoding="utf-8")


def test_smartdraft_review_toc_button_forces_rerun_outstanding_stays_incremental(
    smartdraft_client: TestClient,
) -> None:
    """item 4: an explicit "run toc review" click (``sdReviewToc``) must
    force a re-run even on an already-approved toc — it posts
    ``only_dirty: false`` (whole-draft scope otherwise defaults
    ``only_dirty=True``, which would silently no-op the click). "run
    outstanding checks" (``sdReviewAllOutstanding``) stays the cheap
    incremental pass, ``only_dirty: true``."""
    r = smartdraft_client.get("/smartdraft/sdt")
    assert r.status_code == 200
    body = r.text
    outstanding_fn = body[
        body.index("function sdReviewAllOutstanding") : body.index(
            "function sdReviewToc"
        )
    ]
    toc_fn = body[
        body.index("function sdReviewToc") : body.index("function sdConvertCites")
    ]
    assert "only_dirty: true" in outstanding_fn
    assert "only_dirty: false" in toc_fn


def test_smartdraft_reader_uses_shared_draft_edit(
    smartdraft_client: TestClient,
) -> None:
    """gripe 56746: the focus para's inline editor is the SHARED
    ``draft_editors.draft_edit`` macro (drafts/_editors.html.j2) — the same
    ProseMirror rich editor + `[`-citation autocomplete the classic /drafts
    reader uses — not smartdraft's old plain-``smartEdit`` textarea."""
    r = smartdraft_client.get("/smartdraft/sdt")
    assert r.status_code == 200
    body = r.text
    # the shared component's mount markers
    assert "window.__mountDraftPM" in body  # ProseMirror bootstrap loaded
    assert "draftEdit(" in body  # the shared Alpine component, instantiated
    assert 'x-ref="pm"' in body  # the ProseMirror mount point
    assert "pm-ac" in body  # the `[` citation-autocomplete dropdown CSS
    # the retired smartdraft-only plain-textarea editor is gone
    assert "smartEdit(" not in body


def test_smartdraft_reader_table_focus_uses_shared_table_editor(
    smartdraft_client: TestClient,
) -> None:
    """gripe 56746: focusing a ``chunk_kind='table'`` chunk renders the
    SHARED ``draft_editors.draft_table_editor`` grid (⊞ edit table) — the
    same structured editor the classic /drafts reader uses — instead of the
    raw pipe-markdown text smartdraft rendered before tables had their own
    editor here."""
    table_dc = handle_registry.format_handle("draft", 4, chunk=True)
    r = smartdraft_client.get(f"/smartdraft/sdt?focus={table_dc}")
    assert r.status_code == 200
    body = r.text
    assert "tableEditor(" in body  # the shared Alpine component, instantiated
    assert "⊞ edit table" in body
    # the recovered table renders as a real <table>, not raw pipe-markdown
    assert "<table" in body
    assert "<th" in body and ">A<" in body and ">B<" in body


def test_needs_items_reads_dict_keys_not_attributes(monkeypatch) -> None:
    """Regression: ``_needs_items`` walks ``_work_items``' dict rows. The old
    code read them with ``getattr(w, "todo_id", None)`` — attribute access on
    a dict always misses, so every row silently defaulted to
    ``todo_id=None``/``title=""``/``status="open"`` and the "Needs · in-flight"
    pane rendered a blank row linking to ``/r/todo/None``. This pins the fix:
    real dict values must survive the walk, including the last job's status
    and the blocked/no-jobs fallback."""
    from precis_web.routes import drafts as drafts_mod
    from precis_web.routes.smartdraft import _needs_items

    work_items = [
        {
            "todo_id": 4242,
            "title": "Fix the intro paragraph",
            "blocked": True,
            "jobs": [],
            "asks": [{"tag": "clarify", "question": "Which section?"}],
            "ask_tags": ["clarify"],
        },
        {
            "todo_id": 4343,
            "title": "Rewrite the conclusion",
            "blocked": False,
            "jobs": [
                {"id": 1, "status": "done", "reason": None},
                {"id": 2, "status": "running", "reason": None},
            ],
            "asks": [],
            "ask_tags": [],
        },
    ]
    monkeypatch.setattr(drafts_mod, "_work_items", lambda store, ref_id: work_items)

    rows = _needs_items(store=None, ref_id=700)

    assert len(rows) == 2
    blocked_row, running_row = rows

    # The real todo_id must survive — old getattr-based code always yielded
    # None here (attribute access on a dict never finds "todo_id").
    assert blocked_row["todo_id"] == 4242
    assert blocked_row["title"] == "Fix the intro paragraph"
    assert blocked_row["blocked"] is True
    # No jobs + blocked=True -> "blocked" fallback, not the old "open" default.
    assert blocked_row["status"] == "blocked"
    assert blocked_row["asks"] == ["Which section?"]

    assert running_row["todo_id"] == 4343
    assert running_row["title"] == "Rewrite the conclusion"
    assert running_row["blocked"] is False
    # Status comes from the LAST job, not the first or a hardcoded default.
    assert running_row["status"] == "running"
    assert running_row["asks"] == []


def test_smartdraft_reader_figure_focus_renders_image_and_clearance_badge(
    smartdraft_client: TestClient,
) -> None:
    """gripe 56668: focusing a ``chunk_kind='figure'`` chunk renders the
    SHARED ``draft_figures.figure_media`` image + ``clearance_badge`` (the
    same markup the classic /drafts reader uses) — not the raw caption text
    smartdraft rendered before figures had their own render here."""
    fig_dc = handle_registry.format_handle("draft", 5, chunk=True)
    r = smartdraft_client.get(f"/smartdraft/sdt?focus={fig_dc}")
    assert r.status_code == 200
    body = r.text
    # the actual <img> pointed at the blob route (not raw caption text as a
    # <p>) — versioned with the blob's sha256 so a "refresh"-swapped blob
    # busts the browser's 5-minute Cache-Control.
    assert '<img src="/drafts/blob/H000005?v=fixturesha00"' in body
    # origin chip + clearance badge (cleared — an "original" blob-backed figure)
    assert ">original<" in body
    assert "✓ cleared" in body


def test_smartdraft_reader_figure_focus_caption_is_inline_editable(
    smartdraft_client: TestClient,
) -> None:
    """A figure's ``chunks.text`` IS its caption, so focusing one offers the
    SAME ✎ inline text editor a prose block gets (``draft_editors.draft_edit``
    → POST /drafts/{ident}/text) — before this, a caption was the one piece of
    draft prose with no edit path in either reader: the focus pane rendered the
    image plus the clearance form and nothing that could change the words.

    The editor is opened with ``block_keys=false``: a figure chunk also carries
    the image, so Enter-split would strand the picture on a truncated caption
    and Backspace-merge would retire the chunk (figure and all) into the block
    above."""
    fig_dc = handle_registry.format_handle("draft", 5, chunk=True)
    r = smartdraft_client.get(f"/smartdraft/sdt?focus={fig_dc}")
    assert r.status_code == 200
    body = r.text
    # the ✎ trigger, and the shared editor scope wrapping this figure's handle
    assert "✎ edit caption" in body
    assert "draftEdit('sdt', 'H000005'" in body
    # …with the block-boundary keys off (the 6th arg), unlike a prose block
    assert re.search(
        r"draftEdit\('sdt', 'H000005', '[0-9a-f]+', true, false, false\)", body
    )
    # the editing surface is really there (not just the trigger), seeded with
    # the caption text
    assert "Fig 1. A diagram." in body
    # The scope wraps ONLY the caption: draft_edit hides everything it is
    # handed (x-show="!editing"), so a scope around the whole block would
    # black out the picture the moment you clicked ✎ and leave you captioning
    # blind. The <img> must therefore render BEFORE the editor scope opens.
    img = body.index('<img src="/drafts/blob/H000005?v=fixturesha00"')
    assert img < body.index("draftEdit('sdt', 'H000005'")
    # …and the caption stays a direct child of <figure>, as the element
    # requires — the macro's wrapper <div> goes inside the <figcaption>.
    assert "<figcaption" in body[img : body.index("draftEdit('sdt', 'H000005'")]


def test_smartdraft_collaborate_pane_has_figure_upload_and_clearance_list(
    smartdraft_client: TestClient,
) -> None:
    """gripe 56668: the right ("Collaborate") pane exposes the figure-upload
    control (posts to the SAME ``/drafts/{ident}/figure`` endpoint the
    classic reader uses) and a clearance-surfacing list of the draft's
    figures — regardless of which chunk is currently focused."""
    r = smartdraft_client.get("/smartdraft/sdt")
    assert r.status_code == 200
    body = r.text
    # the upload control (shared draft_figures.figure_upload_form)
    assert 'action="/drafts/sdt/figure"' in body
    assert 'name="file"' in body and 'accept="image/*"' in body
    # the clearance-surfacing list (shared draft_figures.clearance_badge,
    # keyed to the fixture figure's own handle — not the current focus)
    assert 'action="/drafts/sdt/figure/H000005/permission"' in body
    assert "Figures · clearance" in body


def test_smartdraft_reader_404s_on_unknown_draft(
    smartdraft_client: TestClient,
) -> None:
    r = smartdraft_client.get("/smartdraft/does-not-exist")
    assert r.status_code == 404


def test_smartdraft_reader_term_abbrev_is_a_resolvable_occurrence_surface(
    smartdraft_client: TestClient,
) -> None:
    """gripe 56690: a term's dedicated ``abbrev`` resolves occurrences on its
    own — a paragraph mentioning *only* the abbreviation ("STL parts cure
    under UV light.") still shows up in the "occurs in N places" rail when
    the term is focused, independent of the long-form ``short`` surface."""
    term_dc = handle_registry.format_handle("draft", 6, chunk=True)
    r = smartdraft_client.get(f"/smartdraft/sdt?focus={term_dc}")
    assert r.status_code == 200
    body = r.text
    long_dc = handle_registry.format_handle("draft", 7, chunk=True)
    abbrev_dc = handle_registry.format_handle("draft", 8, chunk=True)
    unrelated_dc = handle_registry.format_handle("draft", 9, chunk=True)
    # scope to the Collaborate-pane occurrences rail — data-dc also appears
    # (unrelated to this feature) on every left-pane TOC row.
    rail = body[body.index("Occurs in") : body.index("<textarea")]
    assert f'data-dc="{long_dc}"' in rail  # long-form usage
    assert f'data-dc="{abbrev_dc}"' in rail  # abbrev-only usage
    assert f'data-dc="{unrelated_dc}"' not in rail  # no mention, not listed


def test_smartdraft_reader_term_focus_lists_occurrences_as_focus_nav_links(
    smartdraft_client: TestClient,
) -> None:
    """gripe 56690: focusing a ``chunk_kind='term'`` chunk renders its
    occurrences (computed from the already-loaded node set) as smartdraft
    focus-nav links (``?focus=dc<id>`` / ``data-dc``) with a count."""
    term_dc = handle_registry.format_handle("draft", 6, chunk=True)
    r = smartdraft_client.get(f"/smartdraft/sdt?focus={term_dc}")
    assert r.status_code == 200
    body = r.text
    assert "Occurs in 2 places" in body
    long_dc = handle_registry.format_handle("draft", 7, chunk=True)
    assert f"?focus={long_dc}" in body  # a real focus-nav link, not a dead row


def test_smartdraft_reader_non_term_focus_has_no_occurrences_rail(
    smartdraft_client: TestClient,
) -> None:
    """The occurrences rail only renders for an ``is_term`` focus — a plain
    paragraph focus shows no "Occurs in" section."""
    r = smartdraft_client.get("/smartdraft/sdt")  # default focus: first body para
    assert r.status_code == 200
    assert "Occurs in" not in r.text


def _term_node(
    idx: int, *, short: str, abbrev: str | None, text: str
) -> smartdraft.ChunkNode:
    return smartdraft.ChunkNode(
        idx=idx,
        dc=handle_registry.format_handle("draft", 100 + idx, chunk=True),
        base58=f"term{idx}",
        chunk_id=100 + idx,
        depth=1,
        chunk_kind="term",
        text=text,
        summary="",
        keywords=[],
        term_short=short,
        term_abbrev=abbrev,
    )


def _para_node(idx: int, text: str) -> smartdraft.ChunkNode:
    return smartdraft.ChunkNode(
        idx=idx,
        dc=handle_registry.format_handle("draft", 100 + idx, chunk=True),
        base58=f"para{idx}",
        chunk_id=100 + idx,
        depth=1,
        chunk_kind="paragraph",
        text=text,
        summary="",
        keywords=[],
    )


def test_term_occurrences_excludes_definition_prose_and_case_variants() -> None:
    """gripe: ``term_occurrences`` used to also match the term's own
    ``text`` (its DEFINITION, not a lookup surface) and matched
    case-insensitively — both diverge from what
    :func:`precis_web.linkify._highlight_abbrevs` actually highlights, so
    the "occurs in N places" count listed paragraphs with no live
    ``<abbr class="pa">`` highlight. A REALISTIC term (``short`` distinct
    from a genuine prose ``text`` definition, plus an ``abbrev``) should
    count only the ``short``/``abbrev`` usages — not the definition-prose
    mention, and not a differently-cased mention of ``short``."""
    term = _term_node(
        0,
        short="stereolithography",
        abbrev="STL",
        text="a common 3D-printing process",
    )
    nodes = [
        term,
        _para_node(1, "The part was made via stereolithography overnight."),
        _para_node(2, "STL parts cure under UV light."),
        _para_node(3, "This technique is a common 3D-printing process used widely."),
        _para_node(4, "Stereolithography (capitalized) starts a sentence."),
    ]
    occ = smartdraft.term_occurrences(nodes, term)
    assert [n.idx for n in occ] == [1, 2]  # short usage + abbrev usage only
    assert len(occ) == 2
    # neither the definition-prose paragraph nor the case-variant qualifies
    assert 3 not in [n.idx for n in occ]
    assert 4 not in [n.idx for n in occ]


def test_term_surfaces_excludes_definition_text() -> None:
    """``_term_surfaces`` mirrors ``defined_terms``' surface set exactly —
    ``term_short``/``term_abbrev``/``term_surface_forms``, never ``text``
    (the definition prose)."""
    term = _term_node(
        0, short="stereolithography", abbrev="STL", text="a definition, not a surface"
    )
    surfaces = smartdraft._term_surfaces(term)
    assert set(surfaces) == {"stereolithography", "STL"}
    assert "a definition, not a surface" not in surfaces


def test_smartdraft_cited_sources_panel_lists_focus_block_paper_citations(
    smartdraft_client: TestClient,
) -> None:
    """gripe 56635: the Collaborate rail's "Cited sources" panel lists the
    FOCUS block's paper citations as new-tab links (opening the paper
    reader in a new tab so the writing surface stays put) — carrying no
    ``data-dc`` (so the no-reload nav interceptor leaves them alone)."""
    # Default focus is chunk 2, "...see paper:acheson26." (the first body
    # paragraph — see SmartDraftFakeStore._chunks).
    r = smartdraft_client.get("/smartdraft/sdt")
    assert r.status_code == 200
    body = r.text
    assert "Cited sources" in body
    panel = body[body.index("Cited sources") : body.index("Pinned:")]
    assert 'href="/r/paper/acheson26"' in panel
    assert 'target="_blank"' in panel
    assert "data-dc=" not in panel


def test_smartdraft_cited_sources_panel_omitted_when_focus_has_no_cites(
    smartdraft_client: TestClient,
) -> None:
    """A focus block that cites no paper shows no "Cited sources" panel at
    all (mirrors how "Occurs in N places" omits when empty)."""
    no_cite_dc = handle_registry.format_handle("draft", 3, chunk=True)
    r = smartdraft_client.get(f"/smartdraft/sdt?focus={no_cite_dc}")
    assert r.status_code == 200
    assert "Cited sources" not in r.text


def test_smartdraft_links_panel_lists_in_out_edges_and_anchored_flag(
    smartdraft_client: TestClient,
    smartdraft_runtime: FakeRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gripe 178766: the FOCUS chunk's full connectivity — an outbound edge,
    an inbound edge, and a standalone anchored change-request ("flag it in
    the draft" — no project link, no job, so it's otherwise invisible here)
    — all render in the new Links panel, via the SAME
    ``precis_web.draft_links.chunk_links`` data path the classic reader
    assembles from (``store.drafts.chunk_connections`` split by direction +
    ``store.drafts.anchored_todos``)."""
    store = smartdraft_runtime.store  # default focus = chunk 2, handle H000002

    def fake_conns(
        ref_id: object, handles: object
    ) -> dict[str, list[dict[str, object]]]:
        return {
            "H000002": [
                {
                    "relation": "derived-from",
                    "direction": "out",
                    "kind": "memory",
                    "ident": "20",
                    "title": "A decision",
                },
                {
                    "relation": "cites",
                    "direction": "in",
                    "kind": "memory",
                    "ident": "21",
                    "title": "A citing dream",
                },
            ]
        }

    def fake_flags(handles: object) -> dict[str, list[dict[str, object]]]:
        return {
            "H000002": [
                {
                    "ref_id": 99,
                    "title": "tighten this claim",
                    "status": "open",
                    "audit": "",
                }
            ]
        }

    monkeypatch.setattr(store, "chunk_connections", fake_conns)
    monkeypatch.setattr(store, "anchored_todos", fake_flags)

    r = smartdraft_client.get("/smartdraft/sdt")
    assert r.status_code == 200
    body = r.text
    assert "Links" in body
    panel = body[body.index(">Links<") : body.index("Pinned:")]
    assert 'href="/r/memory/20"' in panel and "A decision" in panel  # out
    assert 'href="/r/memory/21"' in panel and "A citing dream" in panel  # in
    assert 'href="/r/todo/99"' in panel and "tighten this claim" in panel  # flag


def test_smartdraft_links_panel_omitted_when_focus_has_no_edges_or_flags(
    smartdraft_client: TestClient,
) -> None:
    """No panel at all when the focus chunk has no edges/flags (the base
    ``SmartDraftFakeStore`` fixture — its ``FakeStore`` defaults both to
    empty)."""
    r = smartdraft_client.get("/smartdraft/sdt")
    assert r.status_code == 200
    assert ">Links<" not in r.text


def test_smartdraft_reader_popover_is_teleported_to_body(
    smartdraft_client: TestClient,
) -> None:
    """gripe 56806: a hover-preview card rendered inside smartdraft's
    overflow-clipped panes must be wrapped in ``<template x-teleport=
    "body">`` (the shared ``linkify._anchor_html`` fix) so it escapes the
    clip — and the page's now-obsolete per-pane "portal-lite" JS mitigation
    (fixed-coords-on-open listener keyed off ``ref-popover-open``, plus the
    ``data-sd-portaled`` reaping hack) must be gone."""
    r = smartdraft_client.get("/smartdraft/sdt")
    assert r.status_code == 200
    body = r.text
    assert 'href="/r/paper/acheson26"' in body  # the ref actually linkified
    assert '<template x-teleport="body">' in body
    assert "ref-popover" in body
    # the card-level pointer-bridge handler (gripe 56806 regression #1)
    assert '@mouseenter="clearTimeout(closeTimer); hovered = true"' in body
    # obsolete smartdraft-only mitigations, superseded by the shared fix
    assert "data-sd-portaled" not in body
    assert "pop.style.position = 'fixed'" not in body
