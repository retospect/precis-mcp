"""Contract tests for :class:`precis.handlers.finding.FindingHandler`.

Exercises the C3 surface: put / get / cite plus the deterministic
pub_id collapse on repeat puts. The chase worker (C5) lives in a
separate test file.
"""

from __future__ import annotations

import re

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, Unsupported
from precis.handlers.finding import FindingHandler
from precis.identity import make_finding_paper_id, make_pub_id
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import mint_hub


def _search(pattern: str, text: str) -> re.Match[str]:
    """``re.search`` narrowed for tests — asserts the pattern actually hit."""
    m = re.search(pattern, text)
    assert m is not None, f"pattern {pattern!r} not found in {text!r}"
    return m


def _make_handler(store):
    """Build a FindingHandler bound to a fresh store."""
    return FindingHandler(hub=Hub(store=store))


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


def _seed_memory(store, *, text: str = "a research note") -> int:
    """Insert a minimal memory ref — a provenance= target for acquisition
    mode (a numeric kind, addressed as ``memory:<id>``)."""
    from precis.store.types import BlockInsert

    ref = store.insert_ref(kind="memory", slug=None, title=text[:80], meta={})
    store.insert_blocks(ref.id, [BlockInsert(pos=0, text=text, meta={})])
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

    def test_only_cited_in_missing_gets_spin_breaker_hint(self, store) -> None:
        """A claim (title+body) with no cited_in is the turn-eating spin
        signature — the agent has no source handle and re-submits the same
        finding every turn. The recovery hint must tell it what to do when
        it has nothing to cite, not just repeat the happy-path example."""
        h = _make_handler(store)
        with pytest.raises(BadInput) as exc:
            h.put(title="a real claim", body="claim + setup prose", cited_in=None)
        hint = str(exc.value.next)
        assert "do NOT resubmit" in hint or "do not resubmit" in hint.lower()
        # points at the real recovery paths, not the generic example
        assert "search(kind='paper'" in hint
        assert "not a finding" in hint.lower()

    def test_reports_all_missing_required_at_once(self, store) -> None:
        """An under-specified put names every missing field in one error
        — not one-per-call, which made the agent round-trip (and burn
        plan_tick turns) fixing them serially."""
        h = _make_handler(store)
        with pytest.raises(BadInput) as exc:
            h.put()  # nothing supplied
        msg = str(exc.value)
        assert "title" in msg and "body" in msg and "cited_in" in msg

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
                "SELECT text, chunk_kind FROM chunks WHERE ref_id = %s ORDER BY ord",
                (ref_id,),
            ).fetchone()
        assert row is not None
        assert row[0] == "2.4 kV held for 30 s on Si/SiO2 MOSCAPs (Cu, N2)."
        assert row[1] == "finding_body"

        # derived-from link to the paper chunk.
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT dst_ref_id, relation FROM links WHERE src_ref_id = %s",
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
        pub_id = _search(r"pub_id=(\w+)", resp.body).group(1)

        expected_paper_id = make_finding_paper_id(body, scope, "fischer13")
        expected_pub_id = make_pub_id(expected_paper_id)
        assert pub_id == expected_pub_id


# ── parent_id wiring (lit-hunt auto_check linchpin) ─────────────────


def _finding_parent_id(store, ref_id: int) -> int | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT parent_id FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    return None if row is None else row[0]


class TestParentWiring:
    """A finding minted inside a literature-hunt tick MUST be parented on
    the lit-hunt todo, or the ``all_child_findings_resolved`` auto_check
    (which walks ``parent_id = <todo> AND kind='finding'``) never sees it
    and the hunt re-ticks forever. Mirrors TodoHandler's env auto-inject."""

    def _seed_todo(self, store) -> int:
        ref = store.insert_ref(kind="todo", slug=None, title="Lit hunt", meta={})
        return ref.id

    def test_explicit_parent_id_is_honoured(self, store) -> None:
        _seed_paper(store)
        todo_id = self._seed_todo(store)
        h = _make_handler(store)
        resp = h.put(title="t", body="b", cited_in="miller23a", parent_id=todo_id)
        fid = int(_search(r"id=(\d+)", resp.body).group(1))
        assert _finding_parent_id(store, fid) == todo_id

    def test_parent_auto_injected_from_current_todo_env(
        self, store, monkeypatch
    ) -> None:
        _seed_paper(store)
        todo_id = self._seed_todo(store)
        monkeypatch.setenv("PRECIS_CURRENT_TODO", str(todo_id))
        h = _make_handler(store)
        resp = h.put(title="t", body="b", cited_in="miller23a")
        fid = int(_search(r"id=(\d+)", resp.body).group(1))
        assert _finding_parent_id(store, fid) == todo_id

    def test_no_env_no_parent_lands_as_root(self, store, monkeypatch) -> None:
        monkeypatch.delenv("PRECIS_CURRENT_TODO", raising=False)
        _seed_paper(store)
        h = _make_handler(store)
        resp = h.put(title="t", body="b", cited_in="miller23a")
        fid = int(_search(r"id=(\d+)", resp.body).group(1))
        assert _finding_parent_id(store, fid) is None

    def test_non_integer_parent_id_rejected(self, store) -> None:
        h = _make_handler(store)
        with pytest.raises(BadInput) as exc:
            h.put(title="t", body="b", cited_in="miller23a", parent_id="nope")
        assert "parent_id" in str(exc.value)


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

        first_id = int(_search(r"id=(\d+)", first.body).group(1))
        second_id = int(_search(r"id=(\d+)", second.body).group(1))
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
        cu_id = int(_search(r"id=(\d+)", cu.body).group(1))
        ag_id = int(_search(r"id=(\d+)", ag.body).group(1))
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
        ref_id = int(_search(r"id=(\d+)", resp.body).group(1))
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
        ref_id = int(_search(r"id=(\d+)", resp.body).group(1))

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
        ref_id = int(_search(r"id=(\d+)", resp.body).group(1))

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

    def test_hub_get_suppresses_redundant_claim_echo(self, store) -> None:
        """A taproot claim hub's body IS its title verbatim (mint_hub
        writes the sentence to both). The get render should show ``title:``
        once and NOT echo the identical sentence back under ``claim:`` —
        that duplication reads as accidental, not DRY."""
        claim = CanonicalClaim(
            sentence="Graphene exhibits ballistic electron transport at 300 K.",
            scope={"material": "graphene"},
        )
        hub_id = mint_hub(store, claim)
        h = _make_handler(store)
        body = h.get(id=hub_id).body
        assert f"title: {claim.sentence}" in body
        assert "\nclaim:\n" not in body  # no redundant echo block

    def test_plain_finding_still_shows_claim_body(self, store) -> None:
        """A non-hub finding's body carries the setup envelope, so it
        differs from the title and the ``claim:`` block still renders."""
        _seed_paper(store)
        h = _make_handler(store)
        resp = h.put(
            title="short title",
            body="the full claim plus its setup context, longer than the title",
            scope={},
            cited_in="miller23a",
        )
        ref_id = int(_search(r"id=(\d+)", resp.body).group(1))
        body = h.get(id=ref_id).body
        assert "claim:" in body
        assert "setup context" in body


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
        ref_id = int(_search(r"id=(\d+)", resp.body).group(1))
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

    def test_default_cohort_excludes_acquiring(self, store) -> None:
        """AC #6: the default search() cohort is an allowlist
        (established + taproot hubs) — an ``acquiring`` finding is
        excluded by construction, no filter edit involved. An explicit
        ``status='acquiring'`` filter does return it."""
        mem_id = _seed_memory(store)
        h = _make_handler(store)
        resp = h.put(
            title="acquiring claim about batteries",
            body="battery claim body awaiting a paper",
            wants=[{"doi": "10.1234/battery-claim"}],
            provenance=f"memory:{mem_id}",
        )
        acquiring_id = int(_search(r"id=(\d+)", resp.body).group(1))

        out_default = h.search(q="batteries")
        assert "acquiring claim about batteries" not in out_default.body

        out_filtered = h.search(q="batteries", status="acquiring")
        assert str(acquiring_id) in out_filtered.body
        assert "acquiring claim about batteries" in out_filtered.body

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


class TestSearchSurfacesHubs:
    """Regression: a taproot claim hub (``TAPROOT:claim``, ``STATUS:canonical``
    — minted by ``taproot/hub.py::mint_hub``) must show up in the DEFAULT
    finding search (no explicit ``status=``) alongside established
    findings, without needing the ``status='*'`` workaround — while an
    ordinary (non-hub) ``STATUS:tracing`` finding stays hidden from that
    same default, and an explicit ``status='established'`` still excludes
    the hub."""

    _CLAIM = CanonicalClaim(
        sentence="Perovskite solar cells degrade rapidly under ultraviolet exposure.",
        scope={"material": "perovskite", "stressor": "UV"},
    )

    def test_default_tags_filter_surfaces_hub(self, store) -> None:
        """``search(tags=['TAPROOT:claim'])`` with no status= returns the
        hub — today the defaulted STATUS:established AND filter wrongly
        excludes it."""
        h = _make_handler(store)
        hub_id = mint_hub(store, self._CLAIM)

        out = h.search(tags=["TAPROOT:claim"])
        assert str(hub_id) in out.body

    def test_default_q_search_surfaces_hub(self, store) -> None:
        """``search(q=<hub text>)`` with no status= returns the hub —
        its title/body is the claim sentence, which is searchable."""
        h = _make_handler(store)
        hub_id = mint_hub(store, self._CLAIM)

        out = h.search(q="Perovskite")
        assert str(hub_id) in out.body

    def test_default_search_still_hides_ordinary_tracing_finding(self, store) -> None:
        """A plain STATUS:tracing (non-hub) finding stays hidden from the
        default search — only hubs are surfaced, not all tracing rows."""
        from precis.store.types import BlockInsert

        h = _make_handler(store)
        hub_id = mint_hub(store, self._CLAIM)

        ref = store.insert_ref(
            kind="paper", slug="perov-src", title="Perovskite source", meta={}
        )
        store.insert_blocks(
            ref.id,
            [BlockInsert(pos=0, text="Perovskite body chunk.", meta={})],
        )
        resp = h.put(
            title="in-flight perovskite claim",
            body="perovskite degradation claim body text",
            cited_in="perov-src",
        )
        tracing_id = int(_search(r"id=(\d+)", resp.body).group(1))

        out = h.search(q="Perovskite")
        assert str(hub_id) in out.body
        assert f"\n{tracing_id}\t" not in out.body
        assert "in-flight perovskite claim" not in out.body

    def test_explicit_status_established_still_excludes_hub(self, store) -> None:
        """An explicit ``status='established'`` is unchanged — the
        defaulted-only OR does not leak into the explicit single-status
        filter."""
        h = _make_handler(store)
        hub_id = mint_hub(store, self._CLAIM)

        out = h.search(q="Perovskite", status="established")
        assert str(hub_id) not in out.body

        out_tags = h.search(tags=["TAPROOT:claim"], status="established")
        assert str(hub_id) not in out_tags.body


# ── put(supporters=...) — Taproot claim-hub authoring (ADR 0073) ────────


class TestPutSupportersHubMint:
    """``put(kind='finding', title=, supporters=[...])`` with no
    ``cited_in`` mints/converges a Taproot claim hub via
    ``authoring.seed_claim_hub`` — the single write door
    (``taproot/hub.py``) — instead of a chase finding."""

    def test_supporters_mints_claim_hub_with_evidence(self, store) -> None:
        from precis.taproot.seniority import is_claim_hub

        paper = _seed_paper(store, cite_key="miller23a")
        h = _make_handler(store)
        resp = h.put(
            title="amine loading raises CO2 capacity",
            supporters=[{"paper": "miller23a"}],
        )
        m = _search(r"claim hub fi(\d+)", resp.body)
        hub_id = int(m.group(1))
        assert "pub_id=" in resp.body
        assert is_claim_hub(store, hub_id)
        # evidence edge landed paper --role--> hub (not the reverse).
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT relation FROM links WHERE src_ref_id = %s AND dst_ref_id = %s",
                (paper, hub_id),
            ).fetchone()
        assert row is not None
        assert row[0] in ("establishes", "corroborates", "contradicts")

    def test_supporters_converges_onto_existing_hub(self, store) -> None:
        """A second put for the same claim sentence attaches a new
        supporter to the SAME hub rather than minting a duplicate."""
        paper_a = _seed_paper(store, cite_key="paper-a")
        paper_b = _seed_paper(store, cite_key="paper-b")
        h = _make_handler(store)
        sentence = "amine loading raises CO2 capacity"
        r1 = h.put(title=sentence, supporters=[{"paper": "paper-a"}])
        r2 = h.put(title=sentence, supporters=[{"paper": "paper-b"}])
        hub1 = int(_search(r"claim hub fi(\d+)", r1.body).group(1))
        hub2 = int(_search(r"claim hub fi(\d+)", r2.body).group(1))
        assert hub1 == hub2
        for paper in (paper_a, paper_b):
            with store.pool.connection() as conn:
                row = conn.execute(
                    "SELECT 1 FROM links WHERE src_ref_id = %s AND dst_ref_id = %s",
                    (paper, hub1),
                ).fetchone()
            assert row is not None

    def test_supporters_and_cited_in_together_rejected(self, store) -> None:
        _seed_paper(store, cite_key="miller23a")
        h = _make_handler(store)
        with pytest.raises(BadInput, match="different modes"):
            h.put(
                title="t",
                supporters=[{"paper": "miller23a"}],
                cited_in="miller23a",
            )

    def test_supporters_with_empty_claim_rejected(self, store) -> None:
        _seed_paper(store, cite_key="miller23a")
        h = _make_handler(store)
        with pytest.raises(BadInput):
            h.put(supporters=[{"paper": "miller23a"}])  # no title, no body

    def test_normal_chase_finding_path_unaffected(self, store) -> None:
        """cited_in-only (no supporters=) still walks the ordinary
        chase-finding creation path unchanged."""
        _seed_paper(store, cite_key="miller23a")
        h = _make_handler(store)
        resp = h.put(title="t", body="b", cited_in="miller23a")
        assert "created finding id=" in resp.body
        assert "STATUS:tracing" in resp.body


class TestPutAcquisitionMode:
    """``put(kind='finding', wants=[...], provenance=...)`` — the third
    mint mode (acquisition-mode findings): a claim whose supporting
    paper isn't in the corpus yet. AC #1 / #2 / #8 (regression)."""

    def test_wants_mints_acquiring_finding_with_linked_stub(self, store) -> None:
        """AC #1: mints STATUS:acquiring, a DREAM:acquire stub linked
        awaits-evidence, and the stub is claimable by fetch_oa (carries
        the doi)."""
        from precis.workers.fetch_oa import claim_stubs_to_fetch

        mem_id = _seed_memory(store)
        h = _make_handler(store)
        resp = h.put(
            title="claim awaiting a paper",
            body="claim body text that needs grounding",
            wants=[{"doi": "10.1234/acquire-test"}],
            provenance=f"memory:{mem_id}",
        )
        assert "STATUS:acquiring" in resp.body
        fid = int(_search(r"id=(\d+)", resp.body).group(1))

        tag_strs = {str(t) for t in store.tags_for(fid)}
        assert "STATUS:acquiring" in tag_strs

        awaits = store.links_for(fid, direction="out", relation="awaits-evidence")
        assert len(awaits) == 1
        stub_id = awaits[0].dst_ref_id
        stub_ref = store.get_ref(kind="paper", id=stub_id)
        assert stub_ref is not None
        assert stub_ref.pdf_sha256 is None  # a bare stub, nothing ingested yet
        stub_tags = {str(t) for t in store.tags_for(stub_id)}
        assert "DREAM:acquire" in stub_tags

        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT id_value FROM ref_identifiers "
                "WHERE ref_id = %s AND id_kind = 'doi'",
                (stub_id,),
            ).fetchone()
        assert row is not None and row[0] == "10.1234/acquire-test"

        # The provenance link (derived-from, the weakened no-thin-air
        # anchor) landed too.
        provenance_links = store.links_for(
            fid, direction="out", relation="derived-from"
        )
        assert any(link.dst_ref_id == mem_id for link in provenance_links)

        # fetch_oa's own claim query picks up the stub (doi-fetchable).
        with store.pool.connection() as conn:
            claimed = claim_stubs_to_fetch(conn, limit=10)
        assert any(c.ref_id == stub_id for c in claimed)

    def test_title_url_want_mints_stub_without_fetchable_id(self, store) -> None:
        """A ``{'title':…,'url':…}`` descriptor is valid (AC #1's other
        shape) but mints a stub fetch_oa can't auto-claim (no doi/arxiv/
        s2) — it waits on the hand-download queue instead."""
        from precis.workers.fetch_oa import claim_stubs_to_fetch

        mem_id = _seed_memory(store)
        h = _make_handler(store)
        resp = h.put(
            title="claim awaiting an unindexed paper",
            body="claim body text",
            wants=[{"title": "Some Preprint", "url": "https://example.org/paper.pdf"}],
            provenance=f"memory:{mem_id}",
        )
        assert "STATUS:acquiring" in resp.body
        fid = int(_search(r"id=(\d+)", resp.body).group(1))
        awaits = store.links_for(fid, direction="out", relation="awaits-evidence")
        assert len(awaits) == 1
        stub_id = awaits[0].dst_ref_id
        with store.pool.connection() as conn:
            claimed = claim_stubs_to_fetch(conn, limit=10)
        assert not any(c.ref_id == stub_id for c in claimed)

    def test_missing_provenance_rejected(self, store) -> None:
        """AC #2: no provenance= is BadInput naming the missing piece."""
        h = _make_handler(store)
        with pytest.raises(BadInput, match="provenance"):
            h.put(
                title="t",
                body="b",
                wants=[{"doi": "10.1234/acquire-test"}],
            )

    def test_empty_wants_rejected(self, store) -> None:
        """AC #2: empty wants= is BadInput naming the missing piece."""
        mem_id = _seed_memory(store)
        h = _make_handler(store)
        with pytest.raises(BadInput, match="wants"):
            h.put(title="t", body="b", wants=[], provenance=f"memory:{mem_id}")

    def test_want_descriptor_missing_identifier_rejected(self, store) -> None:
        """A wants= entry that carries neither doi/arxiv nor title+url
        is rejected."""
        mem_id = _seed_memory(store)
        h = _make_handler(store)
        with pytest.raises(BadInput, match="doi=, arxiv=, or both title="):
            h.put(
                title="t",
                body="b",
                wants=[{"title": "no url here"}],
                provenance=f"memory:{mem_id}",
            )

    def test_wants_and_cited_in_together_rejected(self, store) -> None:
        _seed_paper(store, cite_key="miller23a")
        mem_id = _seed_memory(store)
        h = _make_handler(store)
        with pytest.raises(BadInput, match="separate mode"):
            h.put(
                title="t",
                body="b",
                wants=[{"doi": "10.1234/acquire-test"}],
                cited_in="miller23a",
                provenance=f"memory:{mem_id}",
            )

    def test_wants_and_supporters_together_rejected(self, store) -> None:
        _seed_paper(store, cite_key="miller23a")
        mem_id = _seed_memory(store)
        h = _make_handler(store)
        with pytest.raises(BadInput, match="different modes"):
            h.put(
                title="t",
                wants=[{"doi": "10.1234/acquire-test"}],
                supporters=[{"paper": "miller23a"}],
                provenance=f"memory:{mem_id}",
            )

    def test_ordinary_and_hub_modes_unaffected(self, store) -> None:
        """AC #8 (regression): the ordinary cited_in= mint and the
        taproot supporters= hub mint are unchanged by the new mode."""
        _seed_paper(store, cite_key="miller23a")
        h = _make_handler(store)
        ordinary = h.put(title="t1", body="b1", cited_in="miller23a")
        assert "created finding id=" in ordinary.body
        assert "STATUS:tracing" in ordinary.body

        hub = h.put(
            title="amine loading raises CO2 capacity",
            supporters=[{"paper": "miller23a"}],
        )
        assert "claim hub fi" in hub.body


# ── link(rel=...) — Taproot evidence/refine routing (ADR 0073) ──────────


class TestLinkTaprootRouting:
    """``link(kind='finding', id='fi<hub>', rel=..., target=...)`` — a
    claim-hub source + a Taproot relation routes through the single write
    door (``taproot/hub.py``) rather than a raw ``add_link``; everything
    else (a non-hub source, a non-Taproot relation) falls through to the
    generic :class:`~precis.handlers._numeric_ref.NumericRefHandler.link`.
    """

    def _mint_hub(self, store, *, sentence: str, scope=None) -> int:
        from precis.taproot.canon import CanonicalClaim
        from precis.taproot.hub import mint_hub

        return mint_hub(store, CanonicalClaim(sentence=sentence, scope=scope or {}))

    def test_establishes_on_hub_routes_to_attach_evidence(self, store) -> None:
        hub_id = self._mint_hub(store, sentence="Pd/C catalyzes Suzuki coupling.")
        paper = _seed_paper(store, cite_key="miller23a")
        with store.pool.connection() as conn:
            chunk_id = conn.execute(
                "SELECT chunk_id FROM chunks WHERE ref_id = %s ORDER BY ord LIMIT 1",
                (paper,),
            ).fetchone()[0]
        h = _make_handler(store)
        out = h.link(id=f"fi{hub_id}", target=f"pc{chunk_id}", rel="establishes")
        assert f"fi{hub_id}" in out.body

        # The evidence edge carries the hub-write shape: paper --role--> hub
        # (attach_evidence's direction), not a raw finding->target add_link.
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT relation, src_chunk_id FROM links "
                "WHERE src_ref_id = %s AND dst_ref_id = %s",
                (paper, hub_id),
            ).fetchone()
        assert row is not None
        assert row[0] == "establishes"
        assert row[1] == chunk_id  # grounded at the pc<id> passage
        # No edge the OTHER way (a raw add_link from the handler's generic
        # door would have gone hub -> target instead).
        with store.pool.connection() as conn:
            reverse = conn.execute(
                "SELECT 1 FROM links WHERE src_ref_id = %s AND dst_ref_id = %s",
                (hub_id, paper),
            ).fetchone()
        assert reverse is None

    def test_corroborates_ref_level_target_grounds_whole_paper(self, store) -> None:
        hub_id = self._mint_hub(store, sentence="Pd/C catalyzes Suzuki coupling.")
        paper = _seed_paper(store, cite_key="wholepaper")
        h = _make_handler(store)
        h.link(id=f"fi{hub_id}", target=f"pa{paper}", rel="corroborates")
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT relation, src_chunk_id FROM links "
                "WHERE src_ref_id = %s AND dst_ref_id = %s",
                (paper, hub_id),
            ).fetchone()
        assert row is not None
        assert row[0] == "corroborates"
        assert row[1] is None  # ref-level: no grounding chunk

    def test_establishes_unresolvable_target_raises(self, store) -> None:
        hub_id = self._mint_hub(store, sentence="Pd/C catalyzes Suzuki coupling.")
        h = _make_handler(store)
        with pytest.raises(BadInput):
            h.link(id=f"fi{hub_id}", target="pc999999999", rel="establishes")

    def test_refines_routes_to_link_claims(self, store) -> None:
        original = self._mint_hub(store, sentence="Original claim.")
        sharper = self._mint_hub(store, sentence="Sharper, reworded claim.")
        h = _make_handler(store)
        out = h.link(id=f"fi{sharper}", target=f"fi{original}", rel="refines")
        assert "refines" in out.body
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT relation FROM links WHERE src_ref_id = %s AND dst_ref_id = %s",
                (sharper, original),
            ).fetchone()
        assert row is not None and row[0] == "refines"

    def test_corroborates_remove_deletes_the_grounded_paper_to_hub_edge(
        self, store
    ) -> None:
        """``mode='remove'`` must mirror ``attach_evidence``'s direction
        (paper --role--> hub): attach a chunk-grounded ``corroborates`` edge,
        remove it the same way, and confirm it's actually gone (not a
        silent no-op from probing hub->paper)."""
        from precis.taproot.seniority import derive_evidence

        hub_id = self._mint_hub(store, sentence="Pd/C catalyzes Suzuki coupling.")
        paper = _seed_paper(store, cite_key="removeme")
        with store.pool.connection() as conn:
            chunk_id = conn.execute(
                "SELECT chunk_id FROM chunks WHERE ref_id = %s ORDER BY ord LIMIT 1",
                (paper,),
            ).fetchone()[0]
        h = _make_handler(store)
        h.link(id=f"fi{hub_id}", target=f"pc{chunk_id}", rel="corroborates")
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT relation, src_chunk_id FROM links "
                "WHERE src_ref_id = %s AND dst_ref_id = %s",
                (paper, hub_id),
            ).fetchone()
        assert row is not None and row[1] == chunk_id  # attached + grounded

        out = h.link(
            id=f"fi{hub_id}", target=f"pc{chunk_id}", rel="corroborates", mode="remove"
        )
        assert "removed 1" in out.body

        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM links WHERE src_ref_id = %s AND dst_ref_id = %s "
                "AND relation = 'corroborates'",
                (paper, hub_id),
            ).fetchone()
        assert row is None  # the grounded edge is actually gone

        evidence = derive_evidence(store, hub_id)
        all_edges = (
            evidence.originators + evidence.corroborators + evidence.contradictors
        )
        assert all(e.paper_ref_id != paper for e in all_edges)

    def test_establishes_remove_ref_level_deletes_the_whole_paper_edge(
        self, store
    ) -> None:
        """The ref-level (``pa<id>``) shape removes cleanly too — no
        grounding chunk to match, ``src_pos`` resolves to ``None`` on both
        attach and remove."""
        hub_id = self._mint_hub(store, sentence="Pd/C catalyzes Suzuki coupling.")
        paper = _seed_paper(store, cite_key="removemewhole")
        h = _make_handler(store)
        h.link(id=f"fi{hub_id}", target=f"pa{paper}", rel="establishes")
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM links WHERE src_ref_id = %s AND dst_ref_id = %s "
                "AND relation = 'establishes'",
                (paper, hub_id),
            ).fetchone()
        assert row is not None

        out = h.link(
            id=f"fi{hub_id}", target=f"pa{paper}", rel="establishes", mode="remove"
        )
        assert "removed 1" in out.body

        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM links WHERE src_ref_id = %s AND dst_ref_id = %s "
                "AND relation = 'establishes'",
                (paper, hub_id),
            ).fetchone()
        assert row is None

    def test_refines_remove_falls_through_to_generic_link(self, store) -> None:
        """``rel='refines'`` removal is id->target (the SAME direction the
        generic door handles) — it must fall through to ``super().link``,
        not the HUB_ROLES interception, and still remove the edge."""
        original = self._mint_hub(store, sentence="Original claim.")
        sharper = self._mint_hub(store, sentence="Sharper, reworded claim.")
        h = _make_handler(store)
        h.link(id=f"fi{sharper}", target=f"fi{original}", rel="refines")
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM links WHERE src_ref_id = %s AND dst_ref_id = %s "
                "AND relation = 'refines'",
                (sharper, original),
            ).fetchone()
        assert row is not None

        # mode='remove' falls through to the generic (numeric-only) door —
        # unlike the 'add' path above it does not resolve a 'fi<id>' handle.
        h.link(id=sharper, target=f"fi{original}", rel="refines", mode="remove")
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM links WHERE src_ref_id = %s AND dst_ref_id = %s "
                "AND relation = 'refines'",
                (sharper, original),
            ).fetchone()
        assert row is None

    def test_contradicts_remove_on_non_hub_finding_falls_through_to_generic_link(
        self, store
    ) -> None:
        """A plain (non-hub) finding's ``contradicts`` removal is NOT a
        HUB_ROLES interception target (the source never resolves to a claim
        hub) — falls through to the generic door, removing the ordinary
        finding->target edge."""
        _seed_paper(store, cite_key="source2")
        target_paper = _seed_paper(store, cite_key="rival2")
        h = _make_handler(store)
        resp = h.put(title="t", body="b", cited_in="source2")
        finding_id = int(_search(r"id=(\d+)", resp.body).group(1))
        h.link(id=finding_id, target=f"pa{target_paper}", rel="contradicts")
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM links WHERE src_ref_id = %s AND dst_ref_id = %s",
                (finding_id, target_paper),
            ).fetchone()
        assert row is not None

        h.link(
            id=finding_id, target=f"pa{target_paper}", rel="contradicts", mode="remove"
        )
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM links WHERE src_ref_id = %s AND dst_ref_id = %s",
                (finding_id, target_paper),
            ).fetchone()
        assert row is None

    def test_contradicts_on_non_hub_finding_falls_through_to_generic_link(
        self, store
    ) -> None:
        """A plain chase finding (not a claim hub) using rel='contradicts'
        must NOT try attach_evidence — it goes through the ordinary
        NumericRefHandler.link door, an edge FROM the finding itself."""
        _seed_paper(store, cite_key="source")
        target_paper = _seed_paper(store, cite_key="rival")
        h = _make_handler(store)
        resp = h.put(title="t", body="b", cited_in="source")
        finding_id = int(_search(r"id=(\d+)", resp.body).group(1))

        out = h.link(id=finding_id, target=f"pa{target_paper}", rel="contradicts")
        assert "linked finding" in out.body

        # Generic add_link shape: src=finding, dst=target (the REVERSE of
        # the hub evidence-edge direction paper->hub asserted above).
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT relation FROM links WHERE src_ref_id = %s AND dst_ref_id = %s",
                (finding_id, target_paper),
            ).fetchone()
        assert row is not None and row[0] == "contradicts"


# ── edit(pick_candidate=...) — multi-candidate disambiguation ───────


class TestPickCandidate:
    """The ``edit(kind='finding', id=N, pick_candidate=...)`` verb.

    When the chase reaches a chunk citing multiple references it
    tags the finding ``STATUS:multi_candidate`` and writes one
    ``derived-from`` link per candidate with ``meta.candidate=true``.
    This verb promotes one, drops the others, replaces the chain's
    frontier with the picked target, and flips status back to
    ``tracing`` so the chase advances on the next pass.
    """

    def _seed_multi_candidate(
        self, store, *, candidate_keys: tuple[str, ...]
    ) -> tuple[int, list[int]]:
        """Seed a finding in the ``STATUS:multi_candidate`` shape.

        Returns ``(finding_ref_id, [candidate_ref_id, ...])``.
        """
        from precis.store.types import Tag

        # Source paper (the cite frontier) — the chase started here.
        _seed_paper(store, cite_key="source")
        h = _make_handler(store)
        resp = h.put(title="t", body="b", scope={}, cited_in="source")
        finding_id = int(_search(r"id=(\d+)", resp.body).group(1))

        # Plant candidate papers + their candidate links.
        candidate_ids: list[int] = []
        for ck in candidate_keys:
            cid = _seed_paper(store, cite_key=ck)
            candidate_ids.append(cid)
            store.add_link(
                src_ref_id=finding_id,
                dst_ref_id=cid,
                dst_pos=None,
                relation="derived-from",
                meta={"candidate": True},
            )

        # Flip status to multi_candidate (chase worker does this).
        store.add_tag(
            finding_id,
            Tag.closed("STATUS", "multi_candidate"),
            set_by="chase",
            replace_prefix=True,
        )
        return finding_id, candidate_ids

    def _status_value(self, store, ref_id: int) -> str | None:
        for t in store.tags_for(ref_id):
            if getattr(t, "namespace", None) == "closed" and t.prefix == "STATUS":
                return t.value
        return None

    def _outbound_derived_from(self, store, ref_id: int) -> list:
        return [
            link
            for link in store.links_for(
                ref_id, direction="out", relation="derived-from"
            )
        ]

    def test_pick_by_cite_key_promotes_and_drops_others(self, store) -> None:
        finding_id, cand_ids = self._seed_multi_candidate(
            store, candidate_keys=("miller23a", "fischer13", "wang2020state")
        )
        h = _make_handler(store)
        out = h.edit(id=finding_id, pick_candidate="fischer13")
        assert "picked candidate fischer13" in out.body

        # One outbound derived-from link remains (the original to
        # 'source' is gone — chain frontier was replaced — and the
        # picked candidate became the new frontier link).
        remaining = self._outbound_derived_from(store, finding_id)
        # We expect exactly the picked candidate + the original
        # source-paper link.
        dst_ids = sorted(link.dst_ref_id for link in remaining)
        # 'fischer13' candidate id is index 1.
        fischer_id = cand_ids[1]
        # The non-picked candidates are gone.
        assert cand_ids[0] not in dst_ids
        assert cand_ids[2] not in dst_ids
        # The picked one is present and no longer marked candidate.
        picked = [link for link in remaining if link.dst_ref_id == fischer_id]
        assert picked
        assert (picked[0].meta or {}).get("candidate") is None

        # Status flipped back to tracing.
        assert self._status_value(store, finding_id) == "tracing"

    def test_pick_by_ref_id(self, store) -> None:
        finding_id, cand_ids = self._seed_multi_candidate(
            store, candidate_keys=("a23", "b24")
        )
        h = _make_handler(store)
        out = h.edit(id=finding_id, pick_candidate=cand_ids[0])
        assert "picked candidate" in out.body
        # cand_ids[1] dropped; cand_ids[0] survived.
        dst_ids = {l.dst_ref_id for l in self._outbound_derived_from(store, finding_id)}
        assert cand_ids[0] in dst_ids
        assert cand_ids[1] not in dst_ids

    def test_unknown_candidate_rejected_with_options(self, store) -> None:
        finding_id, _ = self._seed_multi_candidate(
            store, candidate_keys=("known-a", "known-b")
        )
        h = _make_handler(store)
        with pytest.raises(BadInput) as exc:
            h.edit(id=finding_id, pick_candidate="nosuchcite")
        # Error names the available candidates so the agent can retry.
        opts = getattr(exc.value, "options", None) or []
        assert "known-a" in opts and "known-b" in opts

    def test_unknown_ref_id_rejected(self, store) -> None:
        finding_id, cand_ids = self._seed_multi_candidate(
            store, candidate_keys=("a23",)
        )
        h = _make_handler(store)
        with pytest.raises(BadInput, match="not in the candidate list"):
            h.edit(id=finding_id, pick_candidate=99999)

    def test_finding_without_candidates_rejected(self, store) -> None:
        """A finding not in ``STATUS:multi_candidate`` has no candidate
        links — picking is a category error, not a no-op."""
        _seed_paper(store)
        h = _make_handler(store)
        resp = h.put(title="t", body="b", scope={}, cited_in="miller23a")
        rid = int(_search(r"id=(\d+)", resp.body).group(1))
        with pytest.raises(BadInput, match="no candidate links"):
            h.edit(id=rid, pick_candidate="anything")

    def test_chain_frontier_replaced_with_picked_target(self, store) -> None:
        finding_id, cand_ids = self._seed_multi_candidate(
            store, candidate_keys=("a", "b")
        )
        h = _make_handler(store)
        h.edit(id=finding_id, pick_candidate="a")
        ref = store.get_ref(kind="finding", id=finding_id)
        chain = (ref.meta or {}).get("chain") or []
        # The frontier (last hop) now points at the picked target.
        assert chain[-1]["ref_id"] == cand_ids[0]

    def test_pick_candidate_required(self, store) -> None:
        finding_id, _ = self._seed_multi_candidate(store, candidate_keys=("a", "b"))
        h = _make_handler(store)
        with pytest.raises(BadInput, match="requires pick_candidate"):
            h.edit(id=finding_id)

    def test_id_required(self, store) -> None:
        h = _make_handler(store)
        with pytest.raises(BadInput, match="requires id"):
            h.edit(pick_candidate="x")

    def test_pick_by_pub_id(self, store) -> None:
        """``id=`` accepts the agent-facing pub_id as well as a ref_id."""
        finding_id, cand_ids = self._seed_multi_candidate(
            store, candidate_keys=("a", "b")
        )
        ref = store.get_ref(kind="finding", id=finding_id)
        pub_id = (ref.meta or {})["pub_id"]
        h = _make_handler(store)
        out = h.edit(id=pub_id, pick_candidate="a")
        assert "picked candidate a" in out.body

    def test_pick_candidate_path_unaffected_by_title_routing(self, store) -> None:
        """Sanity: adding the ``title=`` retitle door doesn't disturb the
        pre-existing pick_candidate path when ``title`` isn't passed."""
        finding_id, cand_ids = self._seed_multi_candidate(
            store, candidate_keys=("a", "b")
        )
        h = _make_handler(store)
        out = h.edit(id=finding_id, pick_candidate="a")
        assert "picked candidate a" in out.body


# ── edit(title=...) — retitle a TAPROOT:claim hub ────────────────────


class TestRetitleHub:
    """``edit(kind='finding', id=<hub>, title=…)`` routes through
    ``taproot/hub.py::refine_claim_sentence`` for a claim hub; a plain
    finding has no title-edit door."""

    def _finding_body(self, store, ref_id: int) -> str | None:
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT text FROM chunks WHERE ref_id = %s AND ord = 0 "
                "AND chunk_kind = 'finding_body'",
                (ref_id,),
            ).fetchone()
        return row[0] if row else None

    def test_retitle_hub_updates_title_and_body(self, store) -> None:
        hub = mint_hub(
            store,
            CanonicalClaim(
                sentence="Pd/C catalyzes Suzuki coupling at RT.",
                scope={"material": "Pd/C"},
            ),
        )
        h = _make_handler(store)
        new_sentence = "Pd/C reliably catalyzes Suzuki coupling of aryl halides at RT."

        out = h.edit(id=hub, title=new_sentence)

        assert f"retitled claim hub fi{hub}" in out.body
        ref = store.get_ref(kind="finding", id=hub)
        assert ref.title == new_sentence
        assert self._finding_body(store, hub) == new_sentence

    def test_retitle_hub_by_pub_id(self, store) -> None:
        hub = mint_hub(
            store, CanonicalClaim(sentence="Original claim wording.", scope={})
        )
        with store.pool.connection() as conn:
            pub_id = conn.execute(
                "SELECT id_value FROM ref_identifiers "
                "WHERE ref_id = %s AND id_kind = 'pub_id'",
                (hub,),
            ).fetchone()[0]
        h = _make_handler(store)

        out = h.edit(id=pub_id, title="Reworded claim wording.")
        assert f"retitled claim hub fi{hub}" in out.body

    def test_retitle_hub_collision_raises_bad_input(self, store) -> None:
        hub = mint_hub(
            store, CanonicalClaim(sentence="First claim sentence.", scope={})
        )
        mint_hub(store, CanonicalClaim(sentence="Second claim sentence.", scope={}))
        h = _make_handler(store)

        with pytest.raises(BadInput, match="dedup/merge candidate"):
            h.edit(id=hub, title="Second claim sentence.")

    def test_retitle_non_hub_finding_rejected(self, store) -> None:
        """A plain chase finding has no ``edit(title=…)`` door."""
        _seed_paper(store)
        h = _make_handler(store)
        resp = h.put(title="t", body="b", cited_in="miller23a")
        finding_id = int(_search(r"id=(\d+)", resp.body).group(1))

        with pytest.raises(BadInput, match="TAPROOT:claim"):
            h.edit(id=finding_id, title="a new title")

        # Untouched.
        ref = store.get_ref(kind="finding", id=finding_id)
        assert ref.title == "t"

    def test_retitle_requires_id(self, store) -> None:
        h = _make_handler(store)
        with pytest.raises(BadInput, match="requires id"):
            h.edit(title="a new title")


# ── edit(unacquirable_note=...) — trust-surfaces override write path ──


class TestUnacquirableOverride:
    """``edit(kind='finding', id=N, unacquirable_note=...)`` — writes
    ``meta.unacquirable_override`` (the trust-surfaces override door).
    Settable pre-emptively on any lifecycle status."""

    def _seed_finding(self, store) -> int:
        _seed_paper(store)
        h = _make_handler(store)
        resp = h.put(title="t", body="b", scope={}, cited_in="miller23a")
        return int(_search(r"id=(\d+)", resp.body).group(1))

    def test_happy_path_sets_override(self, store) -> None:
        finding_id = self._seed_finding(store)
        h = _make_handler(store)
        out = h.edit(id=finding_id, unacquirable_note="print-only 1962 monograph")
        assert "recorded unacquirable override" in out.body
        assert "print-only 1962 monograph" in out.body

        ref = store.get_ref(kind="finding", id=finding_id)
        override = (ref.meta or {}).get("unacquirable_override")
        assert override is not None
        assert override["note"] == "print-only 1962 monograph"
        assert override["by"] == "agent"
        assert override["at"]  # server-stamped, non-empty

    def test_settable_on_any_status_preemptively(self, store) -> None:
        """Still STATUS:tracing (the chase hasn't given up yet) — the
        override is allowed pre-emptively, not gated to dead_chain."""
        finding_id = self._seed_finding(store)
        h = _make_handler(store)
        h.edit(id=finding_id, unacquirable_note="known print-only up front")
        ref = store.get_ref(kind="finding", id=finding_id)
        assert (ref.meta or {}).get("unacquirable_override") is not None

    def test_empty_note_rejected(self, store) -> None:
        finding_id = self._seed_finding(store)
        h = _make_handler(store)
        with pytest.raises(BadInput, match="non-empty unacquirable_note"):
            h.edit(id=finding_id, unacquirable_note="   ")

    def test_both_kwargs_rejected(self, store) -> None:
        finding_id = self._seed_finding(store)
        h = _make_handler(store)
        with pytest.raises(BadInput, match="exactly one of"):
            h.edit(id=finding_id, pick_candidate="x", unacquirable_note="why")

    def test_dry_run_rejected(self, store) -> None:
        finding_id = self._seed_finding(store)
        h = _make_handler(store)
        with pytest.raises(BadInput, match="does not support dry_run"):
            h.edit(id=finding_id, unacquirable_note="why", dry_run=True)

    def test_render_shows_override(self, store) -> None:
        finding_id = self._seed_finding(store)
        h = _make_handler(store)
        h.edit(id=finding_id, unacquirable_note="print-only 1962 monograph")
        out = h.get(id=finding_id)
        assert "unacquirable override: print-only 1962 monograph" in out.body
        assert "by agent" in out.body


# ── retraction propagation into findings ────────────────────────────


class TestRetractionPropagation:
    """When a paper on a finding's chain is retracted, the finding
    re-grades: STATUS:tracing, meta.retraction_caveats appended,
    human_verified_at cleared, and a ref_events row recorded so
    ``view='log'`` shows why."""

    def _seed_established_finding_with_chain(
        self, store, *, primary_cite: str = "fischer13"
    ) -> tuple[int, int]:
        """Seed a paper, then a finding whose chain landed at it.

        Returns ``(finding_ref_id, primary_paper_ref_id)``.
        """
        from precis.store.types import Tag

        primary_id = _seed_paper(store, cite_key=primary_cite)
        h = _make_handler(store)
        resp = h.put(title="t", body="b", scope={}, cited_in=primary_cite)
        finding_id = int(_search(r"id=(\d+)", resp.body).group(1))

        # Simulate post-chase state: meta carries primary_cite_key
        # + chain points at the primary; STATUS flipped to
        # established; human_verified_at stamped.
        store.update_ref(
            finding_id,
            meta_patch={
                "primary_cite_key": primary_cite,
                "via_cite_keys": [],
                "chain": [{"ref_id": primary_id, "chunk_id": None, "ord": 0}],
            },
        )
        store.add_tag(
            finding_id,
            Tag.closed("STATUS", "established"),
            set_by="chase",
            replace_prefix=True,
        )
        store.set_human_verified(finding_id, by="owner", note="reviewed")
        return finding_id, primary_id

    def _status_value(self, store, ref_id: int) -> str | None:
        for t in store.tags_for(ref_id):
            if getattr(t, "namespace", None) == "closed" and t.prefix == "STATUS":
                return t.value
        return None

    def test_retraction_regrades_established_finding(self, store) -> None:
        """Retracting the primary paper flips the finding back to
        tracing, appends a caveat record, and clears the human
        verification stamp."""
        finding_id, primary_id = self._seed_established_finding_with_chain(store)
        # Sanity: starting state is established + verified.
        assert self._status_value(store, finding_id) == "established"

        n = store.set_retraction_status(
            primary_id,
            status="retracted",
            reason="data fabrication",
            url="https://retractionwatch.com/abc",
        )
        assert n == 1

        # Status flipped back.
        assert self._status_value(store, finding_id) == "tracing"
        # Caveat record appended to meta.
        ref = store.get_ref(kind="finding", id=finding_id)
        assert ref is not None
        caveats = (ref.meta or {}).get("retraction_caveats") or []
        assert len(caveats) == 1
        c = caveats[0]
        assert c["ref_id"] == primary_id
        assert c["handle"] == "fischer13"
        assert c["reason"] == "data fabrication"
        # Human verification cleared.
        assert ref.human_verified_at is None
        assert ref.human_verified_by is None
        assert ref.human_verified_note is None

    def test_retraction_emits_ref_events_audit_row(self, store) -> None:
        finding_id, primary_id = self._seed_established_finding_with_chain(store)
        store.set_retraction_status(primary_id, status="retracted", reason="x")

        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT source, event, payload FROM ref_events "
                "WHERE ref_id = %s ORDER BY event_id DESC LIMIT 1",
                (finding_id,),
            ).fetchone()
        assert row is not None
        source, event, payload = row
        assert source == "retraction_propagation"
        assert event == "regraded_to_tracing"
        assert payload["ref_id"] == primary_id

    def test_unaffected_finding_is_untouched(self, store) -> None:
        """A finding whose chain doesn't include the retracted ref
        stays put — no spurious caveats, no status changes."""
        # Finding A cites fischer13.
        finding_a, _primary_a = self._seed_established_finding_with_chain(
            store, primary_cite="fischer13"
        )
        # Finding B cites a different paper.
        _seed_paper(store, cite_key="otherprimary")
        h = _make_handler(store)
        resp = h.put(title="other", body="b2", scope={}, cited_in="otherprimary")
        finding_b = int(_search(r"id=(\d+)", resp.body).group(1))

        # Retract finding A's primary. B must stay tracing-default
        # but with no caveats.
        primary_a_id = store.get_ref(kind="paper", id="fischer13").id
        n = store.set_retraction_status(primary_a_id, status="retracted", reason="r")
        assert n == 1  # only A regrades

        ref_b = store.get_ref(kind="finding", id=finding_b)
        assert (ref_b.meta or {}).get("retraction_caveats") is None
        # A re-graded; B's initial status is just whatever put left.
        assert self._status_value(store, finding_a) == "tracing"

    def test_propagation_is_idempotent(self, store) -> None:
        """Re-affirming the same retraction doesn't double-stamp
        the caveats list."""
        finding_id, primary_id = self._seed_established_finding_with_chain(store)
        n1 = store.set_retraction_status(primary_id, status="retracted", reason="r1")
        n2 = store.set_retraction_status(primary_id, status="retracted", reason="r2")
        # Second call sees the existing caveat and skips.
        assert n1 == 1
        assert n2 == 0
        ref = store.get_ref(kind="finding", id=finding_id)
        caveats = (ref.meta or {}).get("retraction_caveats") or []
        assert len(caveats) == 1

    def test_clean_status_no_propagation(self, store) -> None:
        """``status=None`` (a clean re-check) only touches
        ``retraction_checked_at`` — no finding regrades."""
        finding_id, primary_id = self._seed_established_finding_with_chain(store)
        n = store.set_retraction_status(primary_id, status=None)
        assert n == 0
        assert self._status_value(store, finding_id) == "established"

    def test_opt_out_via_propagate_false(self, store) -> None:
        """Bulk backfills can disable cascade with
        ``propagate_to_findings=False``."""
        finding_id, primary_id = self._seed_established_finding_with_chain(store)
        n = store.set_retraction_status(
            primary_id,
            status="retracted",
            reason="r",
            propagate_to_findings=False,
        )
        assert n == 0
        assert self._status_value(store, finding_id) == "established"
