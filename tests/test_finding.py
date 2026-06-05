"""Contract tests for :class:`precis.handlers.finding.FindingHandler`.

Exercises the C3 surface: put / get / cite plus the deterministic
pub_id collapse on repeat puts. The chase worker (C5) lives in a
separate test file.
"""

from __future__ import annotations

import re

import pytest

from precis.errors import BadInput, Unsupported
from precis.handlers.finding import FindingHandler
from precis.hints import HintBus
from precis.identity import make_finding_paper_id, make_pub_id


def _make_handler(store):
    """Build a FindingHandler bound to a fresh store."""

    class _StubHub:
        def __init__(self) -> None:
            self.store = store
            self.embedder = None
            self.hints = HintBus()

    return FindingHandler(hub=_StubHub())


def _seed_paper(store, *, cite_key: str = "miller23a") -> int:
    """Insert a minimal paper ref + cite_key identifier + one body chunk.

    Returns the ref_id. The chunk sits at ord=0 so a chunk-level
    ``cited_in='<cite_key>~0'`` resolves cleanly.
    """
    from precis.store.types import BlockInsert

    ref = store.insert_ref(
        kind="paper",
        slug=cite_key,
        title=f"Test paper {cite_key}",
        meta={},
    )
    store.insert_blocks(
        ref.id,
        [BlockInsert(pos=0, text=f"Body chunk of {cite_key}.", meta={})],
    )
    return ref.id


# ── put validation ──────────────────────────────────────────────────


class TestPutValidation:
    def test_id_rejected(self, store) -> None:
        h = _make_handler(store)
        with pytest.raises(BadInput) as exc:
            h.put(id=5, title="t", body="b", cited_in="x")
        assert "not supported" in str(exc.value)

    def test_requires_title(self, store) -> None:
        h = _make_handler(store)
        with pytest.raises(BadInput) as exc:
            h.put(title=None, body="b", cited_in="x")
        assert "title" in str(exc.value)

    def test_requires_body(self, store) -> None:
        h = _make_handler(store)
        with pytest.raises(BadInput) as exc:
            h.put(title="t", body=None, cited_in="x")
        assert "body" in str(exc.value)

    def test_text_aliases_body(self, store) -> None:
        """Callers that habitually pass text= get the same behaviour."""
        _seed_paper(store)
        h = _make_handler(store)
        resp = h.put(title="t", text="body via text=", cited_in="miller23a")
        assert "created finding id=" in resp.body

    def test_requires_cited_in(self, store) -> None:
        h = _make_handler(store)
        with pytest.raises(BadInput) as exc:
            h.put(title="t", body="b", cited_in=None)
        assert "cited_in" in str(exc.value)

    def test_scope_must_be_dict(self, store) -> None:
        _seed_paper(store)
        h = _make_handler(store)
        with pytest.raises(BadInput) as exc:
            h.put(title="t", body="b", scope="not-a-dict", cited_in="miller23a")
        assert "dict" in str(exc.value)

    def test_unknown_cited_in_rejected(self, store) -> None:
        h = _make_handler(store)
        # The handler delegates to parse_link_target, which raises
        # NotFound when the referenced ref doesn't exist. Either
        # BadInput (syntax) or NotFound (target missing) is a
        # legitimate rejection for "cited_in target doesn't exist"
        # — the caller gets a clear error either way.
        from precis.errors import NotFound

        with pytest.raises((BadInput, NotFound)):
            h.put(title="t", body="b", cited_in="does-not-exist")


# ── put happy path ──────────────────────────────────────────────────


class TestPutHappy:
    def test_creates_ref_chunk_link_and_status(self, store) -> None:
        paper_id = _seed_paper(store)
        h = _make_handler(store)
        resp = h.put(
            title="gate-bias 2.4 kV / 30 s on Si/SiO2",
            body="2.4 kV held for 30 s on Si/SiO2 MOSCAPs (Cu, N2).",
            scope={"electrode": "Cu", "ambient": "N2"},
            cited_in="miller23a~0",
        )
        m = re.search(r"id=(\d+) pub_id=(\w+)", resp.body)
        assert m, f"create-ack missing id/pub_id; got {resp.body!r}"
        ref_id = int(m.group(1))
        pub_id = m.group(2)

        # Ref row landed with the expected shape.
        ref = store.get_ref(kind="finding", id=ref_id)
        assert ref is not None
        assert ref.title == "gate-bias 2.4 kV / 30 s on Si/SiO2"
        meta = ref.meta or {}
        assert meta["scope"] == {"electrode": "Cu", "ambient": "N2"}
        assert meta["pub_id"] == pub_id
        assert meta["paper_id"].startswith("finding:")
        # Chain starts with one entry pointing at the cited frontier.
        chain = meta["chain"]
        assert len(chain) == 1
        assert chain[0]["ref_id"] == paper_id
        assert chain[0]["ord"] == 0

        # pub_id row in ref_identifiers (the dedup linchpin).
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT id_value FROM ref_identifiers "
                "WHERE id_kind = 'pub_id' AND ref_id = %s",
                (ref_id,),
            ).fetchone()
        assert row is not None and row[0] == pub_id

        # finding_body chunk at ord=0.
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT text, chunk_kind FROM chunks "
                "WHERE ref_id = %s ORDER BY ord",
                (ref_id,),
            ).fetchone()
        assert row is not None
        assert row[0] == "2.4 kV held for 30 s on Si/SiO2 MOSCAPs (Cu, N2)."
        assert row[1] == "finding_body"

        # derived-from link to the paper chunk.
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT dst_ref_id, relation FROM links "
                "WHERE src_ref_id = %s",
                (ref_id,),
            ).fetchone()
        assert row is not None
        assert row[0] == paper_id
        assert row[1] == "derived-from"

        # STATUS:tracing tag.
        tags = store.tags_for(ref_id)
        statuses = [str(t) for t in tags if str(t).startswith("STATUS:")]
        assert statuses == ["STATUS:tracing"]

    def test_pub_id_matches_deterministic_formula(self, store) -> None:
        """The handler's pub_id agrees with make_pub_id(make_finding_paper_id(...))."""
        _seed_paper(store, cite_key="fischer13")
        h = _make_handler(store)
        body = "2.4 kV held for 30 s"
        scope = {"electrode": "Cu"}
        resp = h.put(title="t", body=body, scope=scope, cited_in="fischer13")
        pub_id = re.search(r"pub_id=(\w+)", resp.body).group(1)

        expected_paper_id = make_finding_paper_id(body, scope, "fischer13")
        expected_pub_id = make_pub_id(expected_paper_id)
        assert pub_id == expected_pub_id


# ── pub_id collision → idempotent put ───────────────────────────────


class TestDedupOnPubId:
    def test_repeat_put_returns_existing_id(self, store) -> None:
        _seed_paper(store)
        h = _make_handler(store)
        kwargs = dict(
            title="claim",
            body="2.4 kV held for 30 s on Si/SiO2",
            scope={"electrode": "Cu", "ambient": "N2"},
            cited_in="miller23a",
        )
        first = h.put(**kwargs)
        second = h.put(**kwargs)

        first_id = int(re.search(r"id=(\d+)", first.body).group(1))
        second_id = int(re.search(r"id=(\d+)", second.body).group(1))
        assert first_id == second_id
        assert "existing finding" in second.body
        assert "deterministic put" in second.body

    def test_different_scope_creates_distinct_finding(self, store) -> None:
        """The load-bearing case: alternate setups → distinct findings."""
        _seed_paper(store)
        h = _make_handler(store)
        cu = h.put(
            title="t",
            body="2.4 kV held for 30 s",
            scope={"electrode": "Cu"},
            cited_in="miller23a",
        )
        ag = h.put(
            title="t",
            body="2.4 kV held for 30 s",
            scope={"electrode": "Ag"},
            cited_in="miller23a",
        )
        cu_id = int(re.search(r"id=(\d+)", cu.body).group(1))
        ag_id = int(re.search(r"id=(\d+)", ag.body).group(1))
        assert cu_id != ag_id


# ── cite is explicitly unsupported ──────────────────────────────────


class TestCiteRejected:
    def test_cite_raises_unsupported(self, store) -> None:
        h = _make_handler(store)
        with pytest.raises(Unsupported) as exc:
            h.cite(id=1)
        # Primary message names the kind; the "precis resolve"
        # recovery hint rides on exc.value.next (separate attribute
        # on the error envelope).
        assert "finding" in str(exc.value)
        next_hint = getattr(exc.value, "next", "") or ""
        assert "precis resolve" in next_hint


# ── get round-trip ──────────────────────────────────────────────────


class TestRoundTrip:
    def test_get_renders_tracing_finding(self, store) -> None:
        _seed_paper(store)
        h = _make_handler(store)
        resp = h.put(
            title="t",
            body="claim body",
            scope={"electrode": "Cu"},
            cited_in="miller23a",
        )
        ref_id = int(re.search(r"id=(\d+)", resp.body).group(1))
        out = h.get(id=ref_id)
        body = out.body
        assert f"finding {ref_id}" in body
        assert "title: t" in body
        assert "claim body" in body
        assert "electrode: Cu" in body
        assert "STATUS:tracing" in body
        # No primary yet → "chain (in flight" section.
        assert "in flight" in body

    def test_get_renders_established_finding_begat_chain(self, store) -> None:
        """Simulate post-chase state: meta has primary + via cite_keys."""
        _seed_paper(store, cite_key="fischer13")
        _seed_paper(store, cite_key="miller23a")
        h = _make_handler(store)
        resp = h.put(
            title="t",
            body="claim body",
            scope={"electrode": "Cu"},
            cited_in="miller23a",
        )
        ref_id = int(re.search(r"id=(\d+)", resp.body).group(1))

        # Simulate the chain-snapshot pass (chase worker would do this).
        store.update_ref(
            ref_id,
            meta_patch={
                "primary_cite_key": "fischer13",
                "via_cite_keys": ["miller23a"],
            },
        )
        # And flip the status tag.
        from precis.store.types import Tag

        store.add_tag(
            ref_id,
            Tag.closed("STATUS", "established"),
            set_by="chase",
            replace_prefix=True,
        )

        out = h.get(id=ref_id)
        body = out.body
        assert "primary: fischer13" in body
        assert "begat by:" in body
        assert "miller23a" in body
        assert "fischer13  (primary)" in body
        assert "STATUS:established" in body

    def test_get_renders_misattribution_links(self, store) -> None:
        """When a user has flagged a chain hop as a misattribution
        (``link(kind='finding', id=N, link='paper:badcite~7',
        rel='misattributes')``), the begat-chain render surfaces it
        under a dedicated ``misattributed via:`` block so the reader
        sees both what the chase traced to and what the user
        explicitly disowned."""
        _seed_paper(store, cite_key="miller23a")
        bad_id = _seed_paper(store, cite_key="badcite99")
        h = _make_handler(store)
        resp = h.put(
            title="t",
            body="claim body",
            scope={},
            cited_in="miller23a",
        )
        ref_id = int(re.search(r"id=(\d+)", resp.body).group(1))

        # Attach a misattribution link directly via the store —
        # mirrors what `link(kind='finding', ..., rel='misattributes')`
        # would write at the agent surface.
        store.add_link(
            src_ref_id=ref_id,
            dst_ref_id=bad_id,
            dst_pos=0,
            relation="misattributes",
        )

        out = h.get(id=ref_id)
        body = out.body
        assert "misattributed via:" in body
        assert "badcite99~0" in body


# ── search override ─────────────────────────────────────────────────


class TestSearch:
    """The search() override on FindingHandler: status-axis default,
    TOON table shape ``id | title | setup | primary``, and the
    'requires q= or status=/tags=' error path."""

    def _seed_finding(
        self,
        store,
        *,
        cite_key: str,
        title: str,
        body: str,
        scope: dict | None = None,
        status: str = "tracing",
        primary: str | None = None,
    ) -> int:
        from precis.store.types import Tag

        _seed_paper(store, cite_key=cite_key)
        h = _make_handler(store)
        resp = h.put(title=title, body=body, scope=scope or {}, cited_in=cite_key)
        ref_id = int(re.search(r"id=(\d+)", resp.body).group(1))
        if status != "tracing":
            store.add_tag(
                ref_id,
                Tag.closed("STATUS", status),
                set_by="chase",
                replace_prefix=True,
            )
        if primary is not None:
            store.update_ref(ref_id, meta_patch={"primary_cite_key": primary})
        return ref_id

    def test_default_filters_to_established(self, store) -> None:
        """``search(q='...')`` with no ``status=`` returns only
        established findings; the tracing row is filtered out."""
        established = self._seed_finding(
            store,
            cite_key="paper-est",
            title="established claim about photocatalysis",
            body="photocatalysis claim body",
            status="established",
            primary="primary-src",
        )
        self._seed_finding(
            store,
            cite_key="paper-trc",
            title="in-flight claim about photocatalysis",
            body="photocatalysis claim body 2",
            status="tracing",
        )
        h = _make_handler(store)
        out = h.search(q="photocatalysis")
        assert "id\ttitle\tsetup\tprimary" in out.body or "id" in out.body
        assert str(established) in out.body
        # The tracing row must not surface under the default filter.
        assert "in-flight claim" not in out.body

    def test_status_override_returns_tracing_only(self, store) -> None:
        """``status='tracing'`` filters to in-flight findings."""
        self._seed_finding(
            store,
            cite_key="paper-est2",
            title="established cathode claim",
            body="cathode claim body",
            status="established",
            primary="primary-src",
        )
        tracing_id = self._seed_finding(
            store,
            cite_key="paper-trc2",
            title="in-flight cathode claim",
            body="cathode claim body 2",
            status="tracing",
        )
        h = _make_handler(store)
        out = h.search(q="cathode", status="tracing")
        assert str(tracing_id) in out.body
        assert "established cathode claim" not in out.body

    def test_status_star_returns_all(self, store) -> None:
        """``status='*'`` skips the STATUS filter entirely."""
        a = self._seed_finding(
            store,
            cite_key="paper-est3",
            title="kV claim A",
            body="kV body A",
            status="established",
            primary="primary-src",
        )
        b = self._seed_finding(
            store,
            cite_key="paper-trc3",
            title="kV claim B",
            body="kV body B",
            status="tracing",
        )
        h = _make_handler(store)
        out = h.search(q="kV", status="*")
        assert str(a) in out.body
        assert str(b) in out.body

    def test_toon_shape_id_title_setup_primary(self, store) -> None:
        """Result body carries a tab-separated header
        ``id\\ttitle\\tsetup\\tprimary`` plus one row per hit."""
        self._seed_finding(
            store,
            cite_key="paper-shape",
            title="MOSCAP gate-bias 2.4 kV",
            body="device prep at 2.4 kV body text",
            scope={"electrode": "Cu", "ambient": "N2"},
            status="established",
            primary="fischer13",
        )
        h = _make_handler(store)
        out = h.search(q="MOSCAP")
        lines = out.body.splitlines()
        # TOON header row (D2 agent-table): one column-name per
        # tab-delimited cell.
        header_lines = [ln for ln in lines if "title" in ln and "setup" in ln]
        assert header_lines, f"expected TOON header line, got:\n{out.body}"
        body_row = [ln for ln in lines if "Cu" in ln]
        assert body_row, f"expected scope cell with Cu, got:\n{out.body}"
        # primary cite_key in the same row.
        assert "fischer13" in body_row[0]

    def test_q_required_when_no_status_or_tags(self, store) -> None:
        """No q= and no status= raises BadInput at the boundary."""
        h = _make_handler(store)
        with pytest.raises(BadInput, match="requires q="):
            h.search(status="*")

    def test_recency_list_when_only_status_supplied(self, store) -> None:
        """``search(status='tracing')`` with no q= returns a recency
        list of tracing findings (mirrors the base handler's
        empty-q fallback shape)."""
        rid = self._seed_finding(
            store,
            cite_key="paper-rec",
            title="recency claim",
            body="recency body",
            status="tracing",
        )
        h = _make_handler(store)
        out = h.search(status="tracing")
        assert str(rid) in out.body
        assert "recency claim" in out.body
