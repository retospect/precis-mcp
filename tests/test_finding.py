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
        with pytest.raises(BadInput):
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
        msg = str(exc.value)
        assert "finding" in msg
        assert "precis resolve" in msg


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
