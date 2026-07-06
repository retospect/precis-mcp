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
        pub_id = re.search(r"pub_id=(\w+)", resp.body).group(1)

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
        fid = int(re.search(r"id=(\d+)", resp.body).group(1))
        assert _finding_parent_id(store, fid) == todo_id

    def test_parent_auto_injected_from_current_todo_env(
        self, store, monkeypatch
    ) -> None:
        _seed_paper(store)
        todo_id = self._seed_todo(store)
        monkeypatch.setenv("PRECIS_CURRENT_TODO", str(todo_id))
        h = _make_handler(store)
        resp = h.put(title="t", body="b", cited_in="miller23a")
        fid = int(re.search(r"id=(\d+)", resp.body).group(1))
        assert _finding_parent_id(store, fid) == todo_id

    def test_no_env_no_parent_lands_as_root(self, store, monkeypatch) -> None:
        monkeypatch.delenv("PRECIS_CURRENT_TODO", raising=False)
        _seed_paper(store)
        h = _make_handler(store)
        resp = h.put(title="t", body="b", cited_in="miller23a")
        fid = int(re.search(r"id=(\d+)", resp.body).group(1))
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
        finding_id = int(re.search(r"id=(\d+)", resp.body).group(1))

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
        rid = int(re.search(r"id=(\d+)", resp.body).group(1))
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
        finding_id = int(re.search(r"id=(\d+)", resp.body).group(1))

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
        finding_b = int(re.search(r"id=(\d+)", resp.body).group(1))

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
