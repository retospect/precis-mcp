"""FindingHandler — chain head over a citation chase to a primary source.

A `finding` is the **synthesised endpoint** of a citation chase: the
claim text + its setup context + the chain of `derived-from` links
from the agent's initial citation down to the primary source. It is
the agent's *answer* to "what evidence do we have for X?".

This handler owns the **write door** for findings:

- ``put(title, body, scope, cited_in)`` creates a new finding, the
  ``finding_body`` chunk that holds claim + setup as flowing prose,
  the initial ``derived-from`` link to the cited frontier, and
  tags it ``STATUS:tracing``.
- ``get(id)`` renders the begat-style detail (claim, setup, primary,
  via-chain, status).
- ``search(q, status=...)`` filters by status (default
  ``STATUS:established``) and falls through to the base full-text
  + ANN hybrid.
- ``cite(...)`` is **explicitly not supported** — findings are
  internal certainty records; they never appear in ``\\cite{}``.
  The chase-time placeholder is the finding's ``pub_id`` which
  ``precis resolve`` substitutes at finalisation.

Storage details:

* ``kind='finding'`` is seeded in ``0001_initial.sql`` (originally
  added in the archived ``0004_finding_and_queue_family.sql``).
* The finding's deterministic ``paper_id`` comes from
  :func:`precis.identity.make_finding_paper_id` over
  ``(body, scope, initial_cite_handle)``; the ``pub_id`` is
  ``make_pub_id`` over that. Two agents creating the same finding
  from the same source collapse to one row at the
  ``ref_identifiers (id_kind='pub_id')`` UNIQUE constraint.
* The claim title (``title=`` on put) lives in ``refs.title`` for
  list-view scannability; the body lives in a ``finding_body``
  chunk at ord=0 so it embeds + full-text-searches.
* ``meta.scope`` JSONB carries the structured setup envelope.
* ``meta.chain`` JSONB carries the ordered list of hops the chase
  has walked (filled by the chase worker, one append per pass).
* ``meta.primary_cite_key`` and ``meta.via_cite_keys`` snapshot
  the chain in cite_key form at termination.

The chase worker (C5: ``precis.workers.chase``) does not live here
— this handler only owns the storage door. The worker walks the
``links`` graph + ``chunks`` table directly; it does **not** create
``citation`` records under Path B (B-ii).
"""

from __future__ import annotations

from typing import Any, ClassVar

from psycopg.errors import UniqueViolation

from precis.errors import BadInput, Unsupported
from precis.handlers._link_tag_ops import apply_tag_ops
from precis.handlers._link_target import LinkTarget, parse_link_target
from precis.handlers._numeric_ref import NumericRefHandler
from precis.identity import make_finding_paper_id, make_pub_id
from precis.protocol import KindSpec
from precis.response import Response
from precis.store.types import BlockInsert, Ref, Tag
from precis.utils import handle_registry

_STATUS_NAMESPACE = "STATUS"
_STATUS_TRACING = "tracing"
_DERIVED_FROM = "derived-from"


class FindingHandler(NumericRefHandler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="finding",
        title="Finding",
        description=(
            "Chain head over a citation chase to a primary source. Carries "
            "claim + setup context + the begat chain of derived-from links. "
            "Read for 'what evidence do we have for X under setup Y?'; "
            "written by put() (initial cite) and extended by the chase "
            "worker. Never citable externally — pub_id is a placeholder "
            "that precis resolve substitutes for the primary paper's "
            "cite_key at finalisation."
        ),
        supports_put=True,
        supports_get=True,
        supports_search=True,
        supports_search_hits=False,
        supports_edit=True,
        supports_delete=True,
        supports_tag=True,
        supports_link=True,
        is_numeric=True,
        id_required=False,
        note_like=False,
    )
    kind: ClassVar[str] = "finding"
    sense: ClassVar[str] = "finding"

    # ──────────────────────────────────────────────────────────────────
    # put — create a new finding (idempotent on deterministic pub_id)
    # ──────────────────────────────────────────────────────────────────

    def put(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        title: str | None = None,
        body: str | None = None,
        scope: dict[str, Any] | None = None,
        cited_in: str | None = None,
        parent_id: int | None = None,
        tags: list[str] | None = None,
        link: str | None = None,
        rel: str | None = None,
        mode: str | None = None,
        untags: list[str] | None = None,
        unlink: str | None = None,
        # ``text=`` is accepted as an alias for ``body=`` so callers
        # that habitually pass text on every put (the seven-verb
        # default shape) don't get bounced back.
        text: str | None = None,
        **_kw: Any,
    ) -> Response:
        """Create a finding.

        Required: ``title`` (short claim title, ≤200 chars),
        ``body`` (claim text + setup envelope as flowing prose),
        ``cited_in`` (the starting frontier of the chase, in
        ``<cite_key>[~<ord>]`` or ``kind:identifier[~<ord>]`` form).

        Recommended: ``scope`` (structured setup as a dict — used
        for filtering and for two-agents-collapse dedup; e.g.
        ``{"electrode": "Cu", "ambient": "N2"}``).

        Idempotent under identical inputs: same
        ``(body, scope, cited_in_target)`` → same deterministic
        ``pub_id`` → second call collides at the UNIQUE constraint
        on ``ref_identifiers (id_kind='pub_id')`` and returns the
        existing finding's id.

        Existing-id ``put`` is rejected (mutate via tag/link/delete
        per the seven-verb surface).
        """
        # Argument validation — shared with the base / citation handler
        # so mistakes return sharp errors instead of half-created rows.
        self._reject_mutating_put(
            id=id, mode=mode, untags=untags, unlink=unlink, rel=rel, link=link
        )

        body_text = body if body is not None else text
        # Report EVERY missing required field at once, not one per call.
        # The one-at-a-time raise made an under-specified put bounce
        # repeatedly (title, then body, then cited_in) — a turn-eating
        # retry loop seen across prod plan_ticks (transcript review
        # 2026-06-22). A single error lets the agent fix it in one go.
        missing: list[str] = []
        if not title or not title.strip():
            missing.append("title=<short claim title>")
        if not body_text or not body_text.strip():
            missing.append("body=<claim text + setup as prose>")
        if not cited_in or not str(cited_in).strip():
            missing.append("cited_in=<frontier handle, e.g. miller23a~42>")
        if missing:
            # Spin-breaker: a caller that supplies a claim (title+body) but
            # NO cited_in usually has no corpus source handle to give, so it
            # re-submits the SAME claim every turn — a turn-eating loop seen
            # across MOF/citation plan_ticks (transcript review 2026-07-06:
            # one tick fired the identical finding 6× and never converged).
            # Repeating the happy-path example doesn't help an agent that has
            # nothing to cite; tell it what to do instead.
            only_cited_in = missing == ["cited_in=<frontier handle, e.g. miller23a~42>"]
            if only_cited_in:
                next_hint = (
                    "A finding MUST cite a corpus chunk — do NOT resubmit the "
                    "same claim without cited_in. If the source paper is in "
                    "the corpus, pass its handle: cited_in='miller23a~42' "
                    "(chunk) or 'miller23a' (ref-level). If it is NOT in the "
                    "corpus yet, search(kind='paper', q='…') to find it or "
                    "stub it (put(kind='paper', doi='…')) and cite the "
                    "resulting chunk. If this is your own synthesis with no "
                    "single source, it is NOT a finding — write it into the "
                    "draft or record a memory instead."
                )
            else:
                next_hint = (
                    "put(kind='finding', "
                    "title='gate-bias 2.4 kV / 30 s on Si/SiO2', "
                    "body='Device prep: 2.4 kV applied for 30 s on Si/SiO2 "
                    "MOSCAPs with Cu top contact, N2 ambient.', "
                    "scope={'electrode':'Cu','ambient':'N2'}, "
                    "cited_in='miller23a~42')  "
                    "— cited_in is the frontier paper chunk the claim "
                    "starts from (ref-level 'miller23a', chunk-level "
                    "'miller23a~42', or 'paper:miller23a')"
                )
            raise BadInput(
                "put(kind='finding') requires " + ", ".join(missing),
                next=next_hint,
            )
        if scope is not None and not isinstance(scope, dict):
            raise BadInput(
                f"scope must be a dict, got {type(scope).__name__}",
                next="scope={'electrode': 'Cu', 'ambient': 'N2', ...}",
            )
        assert body_text is not None  # narrowed by the `missing` guard above

        # Auto-inject parent_id from the runtime context
        # (PRECIS_CURRENT_TODO env), mirroring TodoHandler.put. A
        # finding minted inside a literature-hunt tick MUST be parented
        # on that lit-hunt todo: the ``all_child_findings_resolved``
        # auto_check walks ``parent_id = <todo> AND kind='finding'`` to
        # decide when the hunt is done. Without this the finding lands
        # as an orphan root, the evaluator never sees it, the todo never
        # closes, and dispatch re-ticks the hunt forever (no draft
        # progress). The interactive/root case still works: no env set →
        # parent_id stays None.
        if parent_id is None:
            from precis.utils.workspace import current_todo_from_env

            parent_id = current_todo_from_env()
        parent_int: int | None = None
        if parent_id is not None:
            try:
                parent_int = parent_id if isinstance(parent_id, int) else int(parent_id)
            except (TypeError, ValueError) as exc:
                raise BadInput(
                    f"parent_id must be an integer, got {parent_id!r}",
                    next="parent_id=<int> (the parent todo's id)",
                ) from exc

        # Resolve the cited target. parse_link_target handles
        # kind:identifier and kind:identifier~N forms; bare handles
        # (no kind prefix) default to 'paper:'.
        target = self._resolve_cited_in(str(cited_in).strip())

        # Use the target ref's stable handle (cite_key, falls back
        # to ref_id) as the deterministic input to make_finding_paper_id.
        # Two agents citing the same source chunk under the same setup
        # collide on the resulting pub_id — that's the design intent.
        target_ref = (
            self.store.get_ref_by_id(target.ref_id)
            if hasattr(self.store, "get_ref_by_id")
            else None
        )
        # Fall back to a direct query when the helper isn't available.
        if target_ref is None:
            target_ref = self._fetch_ref_any_kind(target.ref_id)
        target_handle = target_ref.slug or f"ref:{target.ref_id}"

        paper_id = make_finding_paper_id(
            body_text=body_text,
            scope=scope or {},
            initial_cite_pub_id=target_handle,
        )
        pub_id = make_pub_id(paper_id)

        # Resolve the optional extra link target before the tx so an
        # unknown target fails before we touch the row. User ``tags=``
        # go through the shared ``apply_tag_ops`` inside the tx (a bad
        # tag rolls the create back atomically).
        extra_target: LinkTarget | None = None
        extra_relation: str = rel or "cites"
        if link is not None:
            extra_target = parse_link_target(link, store=self.store)

        body_clean = body_text.strip()
        title_clean = title.strip()[:200]

        meta: dict[str, Any] = {
            "scope": scope or {},
            "paper_id": paper_id,  # for audit / debugging only
            "pub_id": pub_id,
            "chain": [
                {
                    "ref_id": target.ref_id,
                    "chunk_id": None,  # ord is resolved at chase time
                    "ord": target.pos,
                }
            ],
        }

        # Insert ref + identifiers + body chunk + initial link +
        # status tag all inside one transaction. If anything fails
        # (including the pub_id collision case), the whole thing
        # rolls back — no half-created findings.
        try:
            with self.store.tx() as conn:
                ref = self.store.insert_ref(
                    kind=self.kind,
                    slug=None,
                    title=title_clean,
                    meta=meta,
                    parent_id=parent_int,
                    conn=conn,
                )
                # pub_id row for collision detection + agent-facing
                # placeholder. The UNIQUE constraint on
                # (id_kind, id_value) is what makes repeat puts
                # collapse: a second put with the same inputs
                # raises UniqueViolation which we catch below.
                conn.execute(
                    "INSERT INTO ref_identifiers "
                    "(id_kind, id_value, ref_id, source) "
                    "VALUES (%s, %s, %s, %s)",
                    ("pub_id", pub_id, ref.id, "agent"),
                )
                # finding_body chunk at ord=0 (Path B: one body
                # chunk; setup folded into prose).
                self.store.insert_blocks(
                    ref.id,
                    [
                        BlockInsert(
                            pos=0,
                            text=body_clean,
                            meta={"chunk_kind": "finding_body"},
                        )
                    ],
                    conn=conn,
                )
                # STATUS:tracing — closed namespace, one value per
                # ref. Replace any existing STATUS tag (defensive;
                # shouldn't exist on a fresh ref).
                self.store.add_tag(
                    ref.id,
                    Tag.closed(_STATUS_NAMESPACE, _STATUS_TRACING),
                    set_by="agent",
                    replace_prefix=True,
                    conn=conn,
                )
                apply_tag_ops(
                    self.store, self.kind, ref.id, tags=tags, untags=None, conn=conn
                )
                # Initial derived-from link to the cited frontier.
                # This is the chase worker's starting point.
                self.store.add_link(
                    src_ref_id=ref.id,
                    dst_ref_id=target.ref_id,
                    dst_pos=target.pos,
                    relation=_DERIVED_FROM,
                    conn=conn,
                )
                # Optional extra link from link= kwarg (D3 shortcut).
                if extra_target is not None:
                    self.store.add_link(
                        src_ref_id=ref.id,
                        dst_ref_id=extra_target.ref_id,
                        dst_pos=extra_target.pos,
                        relation=extra_relation,
                        conn=conn,
                    )
        except UniqueViolation:
            # Collision on pub_id: this finding already exists.
            # Look up the existing ref_id and return it so the
            # caller sees a deterministic "exists" result.
            return self._collision_response(pub_id)

        return Response(
            body=(
                f"created finding id={ref.id} pub_id={pub_id}\n"
                f"title: {title_clean}\n"
                f"frontier: {target.raw}\n"
                f"status: STATUS:{_STATUS_TRACING}\n"
                f"placeholder: [{pub_id}] (use in text; precis resolve "
                f"substitutes the primary cite_key once STATUS:established)"
            )
        )

    # ──────────────────────────────────────────────────────────────────
    # search — status-filtered TOON table
    # ──────────────────────────────────────────────────────────────────

    def search(  # type: ignore[override]
        self,
        *,
        q: str | None = None,
        status: str | None = None,
        tags: list[str] | None = None,
        page_size: int = 10,
        **_kw: Any,
    ) -> Response:
        """Lexical search across findings with a status-axis default.

        ``status=`` is a finding-specific shorthand for filtering by
        the ``STATUS:`` closed-vocab tag. The default
        (``status='established'``) is the natural "what evidence do
        we have for X?" shape — the agent rarely wants in-flight
        rows mixed in. Pass ``status='tracing'`` /
        ``'multi_candidate'`` / ``'dead_chain'`` to inspect each
        cohort, or ``status='*'`` to see all findings regardless.

        The shorthand desugars to ``tags=['STATUS:<value>']`` and
        unions with any explicit ``tags=`` the caller passed, so
        ``search(status='tracing', tags=['topic-co2'])`` works as
        expected.

        Renders results as a TOON table (``id | title | setup |
        primary``) so the agent gets a scannable list — the begat
        chain detail lives behind ``get(kind='finding', id=N)``.
        """
        # Translate status= shorthand to a closed-vocab tag filter,
        # unless the caller asked for "any status" via '*'.
        effective_tags: list[str] = list(tags) if tags else []
        resolved_status = (status if status is not None else "established").strip()
        if resolved_status and resolved_status != "*":
            tag_str = f"STATUS:{resolved_status}"
            if tag_str not in effective_tags:
                effective_tags.append(tag_str)

        # Validate / normalise via the same path as put(tags=...)
        # so a bogus status value surfaces a sharp BadInput at the
        # boundary rather than a silent empty result.
        normalized = Tag.normalize_filter(effective_tags or None, kind=self.kind)

        # No q= → fall back to a recency list filtered by the tag
        # set (mirrors the base NumericRefHandler.search ergonomics).
        if q is None or not q.strip():
            if normalized:
                refs = self.store.list_refs(
                    kind=self.kind, tags=normalized, limit=page_size
                )
                return self._render_finding_table(refs, query=None)
            raise BadInput(
                "search(kind='finding') requires q= or status=/tags=",
                next=(
                    "search(kind='finding', q='2.4 kV gate dielectric') or "
                    "search(kind='finding', status='tracing')"
                ),
            )

        hits = self.store.search_refs_lexical(
            q=q, kind=self.kind, tags=normalized, limit=page_size
        )
        if not hits:
            tag_suffix = (
                f" with status={resolved_status!r}" if resolved_status != "*" else ""
            )
            body = f"no finding matches {q!r}{tag_suffix}"
            from precis.utils.next_block import render_next_section

            nav: list[tuple[str, str]] = [
                (
                    f"search(kind='finding', q={q!r}, status='*')",
                    "drop the status filter",
                ),
                (
                    f"search(kind='finding', q='broader term', status={resolved_status!r})",
                    "loosen the query",
                ),
            ]
            body += render_next_section(nav)
            return Response(body=body)

        refs = [r for r, _rank in hits]
        return self._render_finding_table(refs, query=q)

    def _render_finding_table(self, refs: list[Ref], *, query: str | None) -> Response:
        """Render the finding-search TOON table.

        Shape: ``id | title | setup | primary``. ``setup`` is
        ``meta.scope`` flattened to ``key=value`` pairs; ``primary``
        is ``meta.primary_cite_key`` when the chase has terminated
        (empty for in-flight rows).
        """
        from precis.format import render_agent_table

        if not refs:
            return Response(body="no finding entries match")

        rows: list[dict[str, str]] = []
        for r in refs:
            meta = r.meta or {}
            scope = meta.get("scope") or {}
            setup_str = ", ".join(f"{k}={v}" for k, v in sorted(scope.items()) if v)
            primary = meta.get("primary_cite_key") or ""
            rows.append(
                {
                    "id": str(r.id),
                    "title": r.title,
                    "setup": setup_str,
                    "primary": primary,
                }
            )

        schema = ["id", "title", "setup", "primary"]
        if query is not None:
            header = f"# {len(refs)} finding match(es) for {query!r}"
        else:
            header = f"# {len(refs)} finding(s)"
        body = f"{header}\n\n" + render_agent_table(rows, schema=schema)
        return Response(body=body)

    # ──────────────────────────────────────────────────────────────────
    # view='log' — filter to chase events
    # ──────────────────────────────────────────────────────────────────

    def _event_log_source(self) -> str | None:
        """Findings' view='log' shows the chase decision trail.

        Other ref_events for the same finding (e.g. future
        verifier-subagent runs, manual operator notes) are
        intentionally excluded — readers want the "why is this
        finding's status what it is?" story, not every event ever
        attached to the row.
        """
        return "chase"

    # ──────────────────────────────────────────────────────────────────
    # edit — pick_candidate (multi-candidate disambiguation)
    # ──────────────────────────────────────────────────────────────────

    def edit(  # type: ignore[override]
        self,
        *,
        id: int | str | None = None,
        pick_candidate: str | int | None = None,
        dry_run: bool | str | None = None,
        **_kw: Any,
    ) -> Response:
        """Resolve a ``STATUS:multi_candidate`` finding by picking one cite.

        When the chase reaches a chunk citing multiple references
        (e.g. ``[12,13]``) and can't disambiguate automatically, it
        tags the finding ``STATUS:multi_candidate`` and writes one
        ``derived-from`` link per candidate with
        ``meta.candidate=true``. The user reads the candidates via
        ``get(kind='finding', id=N)``, then promotes one with:

            edit(kind='finding', id=N, pick_candidate='miller23a')
            edit(kind='finding', id=N, pick_candidate=42)   # by ref_id

        Effect:
        * The chosen candidate link loses its ``meta.candidate``
          marker (becomes a regular ``derived-from`` edge).
        * The other candidate links are deleted.
        * The finding's status flips back to ``STATUS:tracing`` so
          the chase advances on the next pass.
        * ``meta.chain``'s frontier entry is replaced with the
          picked target so the next chase pass walks the right path.

        Idempotent — picking the same candidate twice is fine
        (re-flips to tracing, no-op on links).
        """
        if dry_run:
            # edit(kind='finding') resolves a candidate pick (link
            # rewrites + status flip), not a text region — there is no
            # faithful preview yet. Reject loudly rather than silently
            # apply on dry_run (that was a data-loss footgun).
            raise BadInput(
                "edit(kind='finding') does not support dry_run — it promotes a "
                "candidate cite (rewrites links + flips status); omit dry_run to apply",
                next="edit(kind='finding', id=<N>, pick_candidate='<cite_key>')",
            )
        if id is None:
            raise BadInput(
                "edit(kind='finding') requires id=<finding ref_id or pub_id>",
                next=("edit(kind='finding', id=<N>, pick_candidate='<cite_key>')"),
            )
        if pick_candidate is None or (
            isinstance(pick_candidate, str) and not pick_candidate.strip()
        ):
            raise BadInput(
                "edit(kind='finding') requires pick_candidate=<cite_key or ref_id>",
                next=(
                    "pick_candidate='miller23a' (or the candidate's ref_id) — "
                    "see get(kind='finding', id=N) for the candidate list"
                ),
            )

        finding_ref_id = self._resolve_finding_ref_id(id)

        # Pull all candidate links (outbound derived-from with
        # meta.candidate=true). The chase worker writes these as a
        # batch when it hits a multi-cite chunk.
        candidates = [
            link
            for link in self.store.links_for(
                finding_ref_id, direction="out", relation="derived-from"
            )
            if (link.meta or {}).get("candidate") is True
        ]
        if not candidates:
            raise BadInput(
                f"finding id={finding_ref_id} has no candidate links — nothing to pick",
                next=(
                    "get(kind='finding', id=<N>) — the chain may already "
                    "be resolved (STATUS:established) or this finding is "
                    "in a different state"
                ),
            )

        picked_link, other_links = self._match_candidate(
            candidates, pick_candidate=pick_candidate
        )

        with self.store.tx() as conn:
            # Promote the picked link: clear the candidate flag.
            # No store-level helper for "patch one link's meta", so
            # update by primary key directly — the candidate marker
            # was the only meaningful key on these links.
            conn.execute(
                "UPDATE links SET meta = meta - 'candidate' WHERE link_id = %s",
                (picked_link.id,),
            )
            # Drop the losing candidates by primary key (the
            # store-level ``remove_link`` matches endpoint pairs;
            # we have the exact link rows already so this is
            # tighter and skips the chunk_id resolution dance).
            if other_links:
                conn.execute(
                    "DELETE FROM links WHERE link_id = ANY(%s)",
                    ([link.id for link in other_links],),
                )

            # Replace the chain's frontier entry with the picked
            # target so the next chase pass walks from there.
            ref = self.store.get_ref(kind=self.kind, id=finding_ref_id)
            assert ref is not None
            meta = dict(ref.meta or {})
            chain = list(meta.get("chain") or [])
            if chain:
                # The frontier (last hop) is the multi-cite source —
                # swap it for the picked next-hop so the chain reads
                # as "this is what the chase advanced to."
                chain[-1] = {
                    "ref_id": picked_link.dst_ref_id,
                    "chunk_id": None,
                    "ord": picked_link.dst_pos,
                }
                self.store.update_ref(
                    finding_ref_id, meta_patch={"chain": chain}, conn=conn
                )

            # Flip status back to tracing so the chase worker
            # re-claims this row on the next pass.
            self.store.add_tag(
                finding_ref_id,
                Tag.closed(_STATUS_NAMESPACE, _STATUS_TRACING),
                set_by="user",
                replace_prefix=True,
                conn=conn,
            )

        # Resolve a human-friendly handle for the response body.
        picked_ref = self._fetch_ref_any_kind(picked_link.dst_ref_id)
        picked_handle = picked_ref.slug or f"ref:{picked_link.dst_ref_id}"
        return Response(
            body=(
                f"picked candidate {picked_handle} on finding id={finding_ref_id}\n"
                f"dropped {len(other_links)} other candidate(s); "
                f"status flipped to STATUS:{_STATUS_TRACING}\n"
                f"next: precis worker --only chase --once  "
                f"(or wait for the next pass)"
            )
        )

    def _resolve_finding_ref_id(self, raw_id: int | str) -> int:
        """Resolve ``id=`` to a finding ref_id.

        Accepts a numeric ref_id, a numeric-string ref_id, or a
        ``pub_id`` (the agent-facing placeholder shape).
        """
        if isinstance(raw_id, int):
            ref = self.store.get_ref(kind=self.kind, id=raw_id)
            if ref is None:
                raise BadInput(f"no finding with ref_id={raw_id}")
            return raw_id
        s = str(raw_id).strip()
        if s.isdigit():
            return self._resolve_finding_ref_id(int(s))
        # Treat as pub_id.
        with self.store.pool.connection() as conn:
            row = conn.execute(
                "SELECT r.ref_id FROM ref_identifiers ri "
                "JOIN refs r ON r.ref_id = ri.ref_id "
                "WHERE ri.id_kind = 'pub_id' AND ri.id_value = %s "
                "  AND r.kind = 'finding' AND r.deleted_at IS NULL",
                (s,),
            ).fetchone()
        if row is None:
            raise BadInput(f"no finding with pub_id={s!r}")
        return int(row[0])

    def _match_candidate(
        self, candidates: list, *, pick_candidate: str | int
    ) -> tuple[Any, list]:
        """Pick the link matching ``pick_candidate``; return
        ``(picked, others)``. Accepts a cite_key (slug) or ref_id."""
        if isinstance(pick_candidate, int) or (
            isinstance(pick_candidate, str) and pick_candidate.strip().isdigit()
        ):
            target_ref_id = int(pick_candidate)
            picked = [c for c in candidates if c.dst_ref_id == target_ref_id]
            if not picked:
                raise BadInput(
                    f"ref_id={target_ref_id} is not in the candidate list",
                    options=sorted(str(c.dst_ref_id) for c in candidates),
                )
            return picked[0], [c for c in candidates if c.id != picked[0].id]

        # Match by cite_key (slug). Resolve each candidate ref's
        # cite_key once and look the input up against that map.
        target_slug = str(pick_candidate).strip()
        for c in candidates:
            ref = self._fetch_ref_any_kind(c.dst_ref_id)
            if (ref.slug or "") == target_slug:
                return c, [other for other in candidates if other.id != c.id]
        candidate_slugs = sorted(
            (self._fetch_ref_any_kind(c.dst_ref_id).slug or f"ref:{c.dst_ref_id}")
            for c in candidates
        )
        raise BadInput(
            f"no candidate matches pick_candidate={target_slug!r}",
            options=candidate_slugs,
        )

    # ──────────────────────────────────────────────────────────────────
    # cite — explicitly not supported
    # ──────────────────────────────────────────────────────────────────

    def cite(self, *, id: str | int | None = None, **_kw: Any) -> Response:  # type: ignore[override]
        """Findings are not externally citable.

        The finding's role in published text is the
        ``precis resolve`` substitution: at ``put`` time the agent
        drops ``[<pub_id>]`` in their document; at finalisation
        ``precis resolve`` rewrites it to ``\\cite{<primary_cite_key>}``
        once the chase tags the finding ``STATUS:established``.

        Calling ``cite(kind='finding', ...)`` is therefore a
        category error and we raise here so the agent sees a sharp
        error instead of a silent confusion.
        """
        raise Unsupported(
            "kind='finding' does not support cite — findings are "
            "internal certainty records, not citable surfaces",
            next=(
                "use precis resolve <document> to substitute "
                "[<pub_id>] placeholders with \\cite{<primary>} at "
                "document-finalisation time"
            ),
        )

    # ──────────────────────────────────────────────────────────────────
    # _render_one — begat-style detail rendering
    # ──────────────────────────────────────────────────────────────────

    def _render_one(self, ref: Ref, tags: Any) -> str:  # type: ignore[override]
        """Render one finding record in begat style.

        Sections (omitted when empty):
            title:   the short claim title (from refs.title)
            claim:   the finding_body chunk text
            scope:   meta.scope as key=value pairs
            primary: meta.primary_cite_key (when established)
            begat:   meta.via_cite_keys → primary_cite_key chain
            status:  STATUS tag, or 'tracing' if none recorded
            tags:    any non-STATUS tags
        """
        meta = ref.meta or {}
        scope = meta.get("scope") or {}
        chain = meta.get("chain") or []
        primary_cite = meta.get("primary_cite_key")
        via_cite = meta.get("via_cite_keys") or []
        pub_id = meta.get("pub_id")

        lines: list[str] = [f"# finding {ref.id}"]
        if pub_id:
            lines.append(f"_pub_id: {pub_id}  (placeholder for precis resolve)_")
        lines.append("")
        lines.append(f"title: {ref.title}")

        # The claim body lives in the finding_body chunk; pull it
        # via the standard chunks API so we don't duplicate it on
        # the ref itself.
        body_text = self._fetch_body(ref.id)
        if body_text:
            lines.append("")
            lines.append("claim:")
            for ln in body_text.splitlines():
                lines.append(f"  {ln}")

        if scope:
            lines.append("")
            lines.append("scope:")
            for k in sorted(scope):
                lines.append(f"  {k}: {scope[k]}")

        if primary_cite:
            lines.append("")
            lines.append(f"primary: {primary_cite}")
            if via_cite:
                lines.append("begat by:                     (oldest → newest)")
                for c in via_cite:
                    lines.append(f"  {c}")
                lines.append(f"  {primary_cite}  (primary)")
        elif chain:
            lines.append("")
            lines.append(f"chain (in flight, {len(chain)} hop(s)):")
            for hop in chain:
                lines.append(f"  ref_id={hop.get('ref_id')} ord={hop.get('ord')}")

        # User-curated misattribution links (seeded by migration
        # 0004 as the ``misattributes`` relation). These are
        # outbound edges on the finding pointing at refs whose
        # citation chain the user has flagged as wrong. Surfaced
        # alongside the begat chain so a reader sees both "what we
        # traced to" and "what we explicitly disowned."
        misattrib = self.store.links_for(
            ref.id, direction="out", relation="misattributes"
        )
        if misattrib:
            lines.append("")
            lines.append("misattributed via:")
            for link in misattrib:
                target = self._fetch_ref_any_kind(link.dst_ref_id)
                legacy = target.slug or f"ref:{link.dst_ref_id}"
                pos = link.dst_pos
                # ADR 0036: ref-level → record universal handle; block-level
                # keeps the legacy ``slug~pos`` (chunk_id unavailable here).
                if pos is None:
                    addr = handle_registry.try_format(target.kind, target.id) or legacy
                else:
                    addr = f"{legacy}~{pos}"
                lines.append(f"  {addr}")

        status = _extract_status_tag(tags)
        lines.append("")
        lines.append(f"status: STATUS:{status or _STATUS_TRACING}")

        non_status_tags = [
            t
            for t in (tags or [])
            if getattr(t, "namespace", None) != "closed"
            or not str(t).startswith("STATUS:")
        ]
        if non_status_tags:
            lines.append("tags: " + " ".join(str(t) for t in non_status_tags))

        return "\n".join(lines)

    # ──────────────────────────────────────────────────────────────────
    # private helpers
    # ──────────────────────────────────────────────────────────────────

    def _resolve_cited_in(self, raw: str) -> LinkTarget:
        """Parse ``cited_in=`` into a :class:`LinkTarget`.

        Accepts (a corpus handle — the chunk the claim was read in):
        - ``'miller23a'``               — bare cite_key, paper kind implied
        - ``'miller23a~42'``            — bare cite_key + chunk ord
        - ``'paper:miller23a'``         — explicit kind prefix
        - ``'paper:miller23a~42'``      — explicit + chunk

        A bare ``'doi:…'`` / ``'arxiv:…'`` is **rejected** —
        :func:`parse_link_target` only resolves corpus kinds, so a
        not-yet-ingested DOI raises ``unknown kind 'doi' in link
        target``. Stub + ingest the paper first, then point
        ``cited_in`` at its chunk.

        Returns: :class:`LinkTarget` resolved by
        :func:`parse_link_target`. The ``raw`` field carries the
        original input string (useful for diagnostics + the
        create-ack message).
        """
        if ":" not in raw:
            # Bare handle → assume paper kind, the dominant case.
            qualified = f"paper:{raw}"
        else:
            qualified = raw
        try:
            return parse_link_target(qualified, store=self.store)
        except BadInput as exc:
            raise BadInput(
                f"cited_in={raw!r} could not be resolved: {exc}",
                next=(
                    "cited_in accepts cite_key (bare or 'paper:<key>') "
                    "with optional '~<ord>' chunk selector"
                ),
            ) from exc

    def _fetch_ref_any_kind(self, ref_id: int) -> Ref:
        """Look up a ref by id without knowing its kind.

        The store's get_ref API requires kind; parse_link_target
        returns the resolved kind on the LinkTarget so callers can
        round-trip. We re-fetch here to read the slug (cite_key)
        for the deterministic pub_id input.
        """
        from precis.store._mappers import _REFS_COLS, _row_to_ref

        with self.store.pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_REFS_COLS} FROM refs WHERE ref_id = %s "
                "AND deleted_at IS NULL",
                (ref_id,),
            ).fetchone()
        if row is None:
            raise BadInput(
                f"cited_in target ref_id={ref_id} not found",
                next=(
                    "the target was deleted or never existed — find a live one "
                    "with search(kind='paper', q='<topic>') or look up by DOI "
                    "with get(kind='paper', id='<doi>')"
                ),
            )
        return _row_to_ref(row)

    def _fetch_body(self, ref_id: int) -> str | None:
        """Read the ``finding_body`` chunk text for ``ref_id``.

        Returns ``None`` when no such chunk exists (shouldn't
        happen for a real finding but defensive — soft-deleted-
        and-then-undeleted cases could).
        """
        with self.store.pool.connection() as conn:
            row = conn.execute(
                "SELECT text FROM chunks "
                "WHERE ref_id = %s AND chunk_kind = 'finding_body' "
                "ORDER BY ord LIMIT 1",
                (ref_id,),
            ).fetchone()
        return row[0] if row is not None else None

    def _collision_response(self, pub_id: str) -> Response:
        """Resolve a pub_id collision back to the existing finding."""
        with self.store.pool.connection() as conn:
            row = conn.execute(
                "SELECT ref_id FROM ref_identifiers "
                "WHERE id_kind = 'pub_id' AND id_value = %s",
                (pub_id,),
            ).fetchone()
        existing_id = int(row[0]) if row is not None else None
        return Response(
            body=(
                f"existing finding id={existing_id} pub_id={pub_id}\n"
                f"(deterministic put: same (body, scope, cited_in) → same pub_id; "
                "no duplicate created)"
            )
        )


def _extract_status_tag(tags: Any) -> str | None:
    """Return the STATUS:* value if any, else None."""
    for t in tags or []:
        s = str(t)
        if s.startswith("STATUS:"):
            return s.split(":", 1)[1]
    return None


__all__ = ["FindingHandler"]
