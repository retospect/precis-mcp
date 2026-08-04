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

from dataclasses import dataclass
from datetime import UTC, datetime
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
from precis.taproot import authoring, hub, seniority
from precis.utils import handle_registry

_STATUS_NAMESPACE = "STATUS"
_STATUS_TRACING = "tracing"
_STATUS_ACQUIRING = "acquiring"
_DERIVED_FROM = "derived-from"
_AWAITS_EVIDENCE = "awaits-evidence"
# A taproot claim hub (``taproot/hub.py::mint_hub``) is a ``finding`` ref
# tagged this closed value. Hubs are stamped ``STATUS:canonical`` — off the
# chase-status lifecycle (a hub is a canonicalized claim node, not an
# in-flight chase), so they don't pollute the ``tracing`` cohort and
# ``chase.py::claim_tracing_findings`` never re-claims them. Because the
# default (no explicit ``status=``) search cohort below unions on this
# *tag* rather than a status, a minted hub is visible without the
# ``status='*'`` workaround regardless of the hub's status value.
_TAPROOT_CLAIM_TAG = "TAPROOT:claim"


@dataclass(frozen=True, slots=True)
class _WantDescriptor:
    """One parsed ``wants=`` entry (finding-acquisition-mode.md §2).

    Exactly one of ``doi`` / ``arxiv`` / (``title`` and ``url``) is
    guaranteed non-None by :meth:`FindingHandler._parse_want` — ``title``
    and ``url`` may additionally ride along on a doi/arxiv descriptor as
    enrichment (a nicer stub title, an informational landing-page url).
    """

    doi: str | None
    arxiv: str | None
    title: str | None
    url: str | None
    year: int | None


def _clean_str(value: Any) -> str | None:
    """Strip a scalar to ``None`` on empty/whitespace-only/absent."""
    if value is None:
        return None
    s = str(value).strip()
    return s or None


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
        supporters: list[dict[str, Any]] | None = None,
        wants: list[dict[str, Any]] | None = None,
        provenance: str | None = None,
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
        """Create a finding — trimodal.

        **Ordinary (chase) mode.** Required: ``title`` (short claim
        title, ≤200 chars), ``body`` (claim text + setup envelope as
        flowing prose), ``cited_in`` (the starting frontier of the
        chase, in ``<cite_key>[~<ord>]`` or ``kind:identifier[~<ord>]``
        form).

        Recommended: ``scope`` (structured setup as a dict — used
        for filtering and for two-agents-collapse dedup; e.g.
        ``{"electrode": "Cu", "ambient": "N2"}``).

        Idempotent under identical inputs: same
        ``(body, scope, cited_in_target)`` → same deterministic
        ``pub_id`` → second call collides at the UNIQUE constraint
        on ``ref_identifiers (id_kind='pub_id')`` and returns the
        existing finding's id.

        **Acquisition mode** (finding-acquisition-mode.md) — the claim's
        supporting paper isn't in the corpus yet: pass ``wants=`` (a list
        of ``{'doi':…}`` / ``{'arxiv':…}`` / ``{'title':…,'url':…}``
        descriptors, ≥1) and ``provenance=`` (a ref/chunk handle for
        where the claim came from) INSTEAD of ``cited_in=``. Mints
        ``STATUS:acquiring`` plus one ``DREAM:acquire`` paper stub per
        descriptor; the chase worker grounds it once a stub lands a PDF.
        See :meth:`_put_acquiring`.

        **Hub mode** (ADR 0073) — pass ``supporters=`` instead of
        ``cited_in=``/``wants=`` to mint/converge a Taproot claim hub.

        Existing-id ``put`` is rejected (mutate via tag/link/delete
        per the seven-verb surface).
        """
        # Argument validation — shared with the base / citation handler
        # so mistakes return sharp errors instead of half-created rows.
        self._reject_mutating_put(
            id=id, mode=mode, untags=untags, unlink=unlink, rel=rel, link=link
        )

        body_text = body if body is not None else text

        # --- Taproot hub-mint mode (ADR 0073) ---
        # A finding born with paper ``supporters=`` (and no ``cited_in``) is a
        # claim HUB, not a chase target: route through the single write door
        # (``taproot/hub.py`` via ``seed_claim_hub``), which mints/converges the
        # hub and attaches each supporter's ``paper --role--> hub`` evidence
        # edge. The grounding invariant holds by construction — ``seed_claim_hub``
        # REQUIRES paper supporters, so this door can never mint a thin-air hub.
        # ``title=``/``body=`` carry the canonical claim sentence.
        if supporters is not None:
            if cited_in is not None or wants is not None:
                raise BadInput(
                    "hub-mint, chase-finding, and acquisition-mint are "
                    "different modes — pass supporters= (a Taproot claim "
                    "hub grounded in papers) OR cited_in= (a chase finding "
                    "to walk to its primary) OR wants=/provenance= "
                    "(acquisition mode), not more than one.",
                    next="keep one: supporters=[{'paper':'pa5',"
                    "'source_handle':'pc293'}] for a hub, cited_in='pc42' "
                    "for a chase finding, or wants=[{'doi':'10.1/x'}], "
                    "provenance='pc42' for acquisition mode",
                )
            sentence = (body_text or title or "").strip()
            if not sentence:
                raise BadInput(
                    "a claim hub needs its canonical claim sentence — pass "
                    "title=<claim> (or body=<claim>).",
                    next="put(kind='finding', title='amine loading raises CO2 "
                    "capacity', supporters=[{'paper':'pa5',"
                    "'source_handle':'pc293'}])",
                )
            if scope is not None and not isinstance(scope, dict):
                raise BadInput(
                    f"scope must be a dict, got {type(scope).__name__}",
                    next="scope={'system': 'aqueous', ...}",
                )
            result = authoring.seed_claim_hub(
                self.store,
                sentence=sentence,
                scope=scope or {},
                supporters=supporters,
                set_by="agent",
            )
            ung = result["ungrounded"]
            return Response(
                body=(
                    f"claim hub fi{result['hub_ref_id']}  "
                    f"pub_id={result['pub_id']}\n"
                    f"claim: {sentence[:120]}\n"
                    f"evidence: {result['attached']} attached, "
                    f"{result['already']} already present"
                    + (f", {ung} ref-level (ungrounded)" if ung else "")
                    + "\n"
                    f"cite it inline as [fi{result['hub_ref_id']}] — resolves to "
                    "the current derived originator(s) on every render"
                )
            )

        # --- Acquisition mode (finding-acquisition-mode.md) ---
        # A finding born with ``wants=`` (paper descriptors, no
        # ``cited_in=``/``supporters=``) records a claim whose supporting
        # paper(s) aren't in the corpus yet: mint STATUS:acquiring,
        # atomically upsert a DREAM:acquire stub per descriptor, link
        # finding --awaits-evidence--> stub, and let the chase worker
        # ground it once fetch_oa lands a PDF. Checked before the
        # missing-field report below so a caller mixing modes gets a
        # mode-conflict error, not a confusing "missing cited_in" one.
        if wants is not None:
            # supporters is not None is unreachable here: the earlier
            # `if supporters is not None:` block above already catches
            # (and rejects, when wants is also set) any supporters=+wants=
            # combination before control ever reaches this branch.
            if cited_in is not None:
                raise BadInput(
                    "acquisition-mode (wants=) is a separate mode from "
                    "cited_in= (chase finding) and supporters= (taproot "
                    "hub) — pass exactly one.",
                    next=(
                        "wants=[{'doi':'10.1234/xyz'}], provenance='pc42' "
                        "for acquisition mode; OR cited_in='pc42' for an "
                        "ordinary chase finding; OR supporters=[...] for "
                        "a hub"
                    ),
                )
            return self._put_acquiring(
                title=title,
                body_text=body_text,
                scope=scope,
                wants=wants,
                provenance=provenance,
                parent_id=parent_id,
                tags=tags,
                link=link,
                rel=rel,
            )

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
                    "resulting chunk. If instead this is a canonicalized "
                    "cross-paper claim grounded in specific papers, mint a "
                    "Taproot hub: put(kind='finding', title=<claim>, "
                    "supporters=[{'paper':'pa5','source_handle':'pc293'}]). "
                    "If this is your own synthesis with no single source, it "
                    "is NOT a finding — write it into the draft or record a "
                    "memory instead."
                )
            else:
                next_hint = (
                    "put(kind='finding', "
                    "title='gate-bias 2.4 kV / 30 s on Si/SiO2', "
                    "body='Device prep: 2.4 kV applied for 30 s on Si/SiO2 "
                    "MOSCAPs with Cu top contact, N2 ambient.', "
                    "scope={'electrode':'Cu','ambient':'N2'}, "
                    "cited_in='pc42')  "
                    "— cited_in is the frontier paper chunk the claim "
                    "starts from: a chunk handle like 'pc42' (legacy "
                    "'miller23a~42' / 'paper:miller23a' still resolve)"
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
        assert title is not None  # narrowed by the `missing` guard above

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
    # _put_acquiring — acquisition-mode mint (finding-acquisition-mode.md)
    # ──────────────────────────────────────────────────────────────────

    def _put_acquiring(
        self,
        *,
        title: str | None,
        body_text: str | None,
        scope: dict[str, Any] | None,
        wants: list[dict[str, Any]],
        provenance: str | None,
        parent_id: int | None,
        tags: list[str] | None,
        link: str | None,
        rel: str | None,
    ) -> Response:
        """Mint an acquisition-mode finding: a claim whose supporting
        paper(s) aren't in the corpus yet.

        Atomically (one ``store.tx()``): creates the finding
        ``STATUS:acquiring``, upserts a ``DREAM:acquire`` paper stub per
        ``wants=`` descriptor (the existing :meth:`~precis.store.Store.
        upsert_stub_paper` path — the same one ``put(kind='paper')``'s
        ``acquire`` uses), links ``finding --awaits-evidence--> stub`` for
        each, and links ``finding --derived-from--> provenance`` (the
        weakened no-thin-air invariant: traceable to *something* at mint,
        just not yet to corpus evidence). ``chase.py``'s acquiring arm
        polls the linked stubs and grounds the finding once one gains
        chunks; a doi/arxiv stub is separately auto-claimed by
        ``fetch_oa``.
        """
        missing: list[str] = []
        if not title or not title.strip():
            missing.append("title=<short claim title>")
        if not body_text or not body_text.strip():
            missing.append("body=<claim text + setup as prose>")
        if not wants:
            missing.append(
                "wants=[{'doi':…}|{'arxiv':…}|{'title':…,'url':…}, …] "
                "(>=1 descriptor of the paper(s) this claim expects "
                "grounding from)"
            )
        if not provenance or not str(provenance).strip():
            missing.append(
                "provenance=<ref/chunk handle for where this claim came from>"
            )
        if missing:
            raise BadInput(
                "put(kind='finding', wants=...) requires " + ", ".join(missing),
                next=(
                    "put(kind='finding', title='<short claim title>', "
                    "body='<claim text + setup>', "
                    "wants=[{'doi':'10.1234/xyz'}], provenance='pc42')  "
                    "— provenance is the ref/chunk handle where this claim "
                    "came from (a research note, the lit-hunt todo, or the "
                    "citing chunk); each wants= entry is {'doi':…}, "
                    "{'arxiv':…}, or {'title':…,'url':…}"
                ),
            )
        if scope is not None and not isinstance(scope, dict):
            raise BadInput(
                f"scope must be a dict, got {type(scope).__name__}",
                next="scope={'electrode': 'Cu', 'ambient': 'N2', ...}",
            )
        assert title is not None  # narrowed by the `missing` guard above
        assert body_text is not None  # narrowed by the `missing` guard above
        assert provenance is not None  # narrowed by the `missing` guard above

        parsed_wants = [self._parse_want(i, w) for i, w in enumerate(wants)]

        # Resolve provenance up front — a bad handle fails before any
        # write, mirroring the ordinary mode's cited_in resolution.
        provenance_target = parse_link_target(str(provenance).strip(), store=self.store)

        # Auto-inject parent_id from the runtime context (PRECIS_CURRENT_TODO
        # env), same as the ordinary path — a finding minted inside a
        # lit-hunt tick must be parented on that todo so
        # all_child_findings_resolved sees it.
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

        extra_target: LinkTarget | None = None
        extra_relation: str = rel or "cites"
        if link is not None:
            extra_target = parse_link_target(link, store=self.store)

        body_clean = body_text.strip()
        title_clean = title.strip()[:200]

        # Deterministic dedup key: same (body, scope, wants) → same pub_id,
        # mirroring the ordinary mode's cited_in-keyed collapse.
        wants_key = "|".join(
            sorted(
                f"{field}={value}"
                for w in parsed_wants
                for field, value in (
                    ("doi", w.doi),
                    ("arxiv", w.arxiv),
                    ("title", w.title),
                    ("url", w.url),
                )
                if value
            )
        )
        paper_id = make_finding_paper_id(
            body_text=body_clean,
            scope=scope or {},
            initial_cite_pub_id=f"acquire:{wants_key}",
        )
        pub_id = make_pub_id(paper_id)

        meta: dict[str, Any] = {
            "scope": scope or {},
            "paper_id": paper_id,
            "pub_id": pub_id,
            # Empty at mint — chase.py's acquiring arm seeds this once a
            # linked stub is grounded (has chunks); an empty chain here is
            # NOT dead_chain the way it is for the ordinary (tracing) mode.
            "chain": [],
            "wants": [
                {
                    field: value
                    for field, value in (
                        ("doi", w.doi),
                        ("arxiv", w.arxiv),
                        ("title", w.title),
                        ("url", w.url),
                        ("year", w.year),
                    )
                    if value is not None
                }
                for w in parsed_wants
            ],
        }

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
                conn.execute(
                    "INSERT INTO ref_identifiers "
                    "(id_kind, id_value, ref_id, source) "
                    "VALUES (%s, %s, %s, %s)",
                    ("pub_id", pub_id, ref.id, "agent"),
                )
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
                self.store.add_tag(
                    ref.id,
                    Tag.closed(_STATUS_NAMESPACE, _STATUS_ACQUIRING),
                    set_by="agent",
                    replace_prefix=True,
                    conn=conn,
                )
                apply_tag_ops(
                    self.store, self.kind, ref.id, tags=tags, untags=None, conn=conn
                )
                self.store.add_link(
                    src_ref_id=ref.id,
                    dst_ref_id=provenance_target.ref_id,
                    dst_pos=provenance_target.pos,
                    relation=_DERIVED_FROM,
                    conn=conn,
                )
                if extra_target is not None:
                    self.store.add_link(
                        src_ref_id=ref.id,
                        dst_ref_id=extra_target.ref_id,
                        dst_pos=extra_target.pos,
                        relation=extra_relation,
                        conn=conn,
                    )
                stub_lines: list[str] = []
                for w in parsed_wants:
                    identifiers: list[tuple[str, str]] = []
                    if w.doi:
                        identifiers.append(("doi", w.doi))
                    if w.arxiv:
                        identifiers.append(("arxiv", w.arxiv))
                    stub_ref_id, created = self.store.upsert_stub_paper(
                        identifiers=identifiers,
                        title=w.title,
                        year=w.year,
                        set_by="dream",
                        conn=conn,
                    )
                    if created:
                        self.store.add_tag(
                            stub_ref_id,
                            Tag.closed("DREAM", "acquire"),
                            set_by="agent",
                            conn=conn,
                        )
                    if w.url:
                        # Informational only in this build — no fetch leg
                        # reads a bare URL yet (finding-acquisition-mode.md
                        # "Explicitly NOT in scope"); a human sees it via
                        # get(kind='paper', id=<stub>).
                        self.store.update_ref(
                            stub_ref_id,
                            meta_patch={"acquire_url": w.url},
                            conn=conn,
                        )
                    self.store.add_link(
                        src_ref_id=ref.id,
                        dst_ref_id=stub_ref_id,
                        relation=_AWAITS_EVIDENCE,
                        conn=conn,
                    )
                    stub_handle = (
                        handle_registry.try_format("paper", stub_ref_id)
                        or f"ref:{stub_ref_id}"
                    )
                    stub_lines.append(
                        f"  {stub_handle} ({'minted' if created else 'already tracked'})"
                    )
        except UniqueViolation:
            # Collision on pub_id: this finding already exists.
            return self._collision_response(pub_id)

        return Response(
            body=(
                f"created finding id={ref.id} pub_id={pub_id}\n"
                f"title: {title_clean}\n"
                f"provenance: {provenance_target.raw}\n"
                f"status: STATUS:{_STATUS_ACQUIRING}\n"
                f"awaiting evidence from {len(parsed_wants)} paper(s):\n"
                + "\n".join(stub_lines)
                + "\n"
                f"placeholder: [{pub_id}] (use in text; precis resolve "
                f"substitutes the primary cite_key once STATUS:established)"
            )
        )

    def _parse_want(self, index: int, want: Any) -> _WantDescriptor:
        """Parse one ``wants=`` entry into a :class:`_WantDescriptor`.

        Accepts ``{'doi': …}``, ``{'arxiv': …}``, or ``{'title': …, 'url':
        …}`` — a ``title=``/``url=`` may additionally ride along a doi/arxiv
        descriptor as enrichment. Rejects anything matching none of the
        three shapes.
        """
        if not isinstance(want, dict):
            raise BadInput(
                f"wants[{index}] must be a dict, got {type(want).__name__}",
                next="wants=[{'doi': '10.1234/xyz'}] — one descriptor per paper",
            )
        doi = _clean_str(want.get("doi"))
        arxiv = _clean_str(want.get("arxiv"))
        w_title = _clean_str(want.get("title"))
        url = _clean_str(want.get("url"))
        year: int | None = None
        raw_year = want.get("year")
        if raw_year is not None:
            try:
                year = int(raw_year)
            except (TypeError, ValueError):
                year = None
        if not (doi or arxiv or (w_title and url)):
            raise BadInput(
                f"wants[{index}] needs doi=, arxiv=, or both title= and url=",
                next=(
                    "wants=[{'doi':'10.1234/xyz'}] or {'arxiv':'2401.00001'} "
                    "or {'title':'<best-known title>','url':'<landing page>'}"
                ),
            )
        return _WantDescriptor(doi=doi, arxiv=arxiv, title=w_title, url=url, year=year)

    # ──────────────────────────────────────────────────────────────────
    # link — intercept Taproot evidence/refine edges on a claim hub
    # ──────────────────────────────────────────────────────────────────

    def link(  # type: ignore[override]
        self,
        *,
        id: str | int,
        target: str | None = None,
        mode: str = "add",
        rel: str | None = None,
        **_kw: Any,
    ) -> Response:
        """Taproot-aware link door for findings.

        When the source resolves to a ``TAPROOT:claim`` hub and ``rel`` is a
        Taproot relation, route through the single write door
        (``taproot/hub.py``) rather than a raw ``add_link`` — ADR 0073: a raw
        insert for these relations bypasses the role + ``TAPROOT:claim`` guards
        and is a defect.

        - ``rel`` ∈ {establishes, corroborates, contradicts} → attach one
          ``paper --role--> hub`` evidence edge. ``target`` is the supporting
          paper/chunk handle: ``pc<id>`` grounds the edge at that passage,
          ``pa<id>`` lands ref-level. The role is a conservative write-time
          label; the originator/corroborator split is derived at read time
          (``view='evidence'``).
        - ``rel='refines'`` → link this hub as a sharper/reworded version of
          another hub (``target`` a ``fi<id>``); advisory, no evidence flows.

        Anything else — a non-hub finding, a non-Taproot relation, or
        ``mode='remove'`` — falls through to the generic numeric-ref link.
        """
        if mode == "add" and rel in (hub.HUB_ROLES | hub.CLAIM_LINK_RELATIONS):
            try:
                hub_ref_id: int | None = authoring.resolve_hub_ref_id(self.store, id)
            except BadInput:
                # Source isn't a claim hub — a plain finding using a general
                # relation (e.g. ``contradicts``). Use the generic door.
                hub_ref_id = None
            if hub_ref_id is not None:
                if not target or not str(target).strip():
                    raise BadInput(
                        f"link rel={rel!r} on claim hub fi{hub_ref_id} needs a target",
                        next=(
                            "target='pc<id>' (a supporting paper chunk)"
                            if rel in hub.HUB_ROLES
                            else "target='fi<id>' (the coarser hub this refines)"
                        ),
                    )
                tgt = str(target).strip()
                if rel in hub.HUB_ROLES:
                    resolved = self.store.resolve_handle(tgt)
                    if resolved is None:
                        raise BadInput(
                            f"evidence target {tgt!r} is not a resolvable "
                            "paper or paper-chunk handle",
                            next="target='pc<id>' (grounds the edge at that "
                            "passage) or 'pa<id>' (ref-level, whole paper)",
                        )
                    hub.attach_evidence(
                        self.store,
                        hub_ref_id=hub_ref_id,
                        paper_ref_id=resolved.ref_id,
                        role=rel,
                        meta={"source_handle": tgt},
                        set_by="agent",
                    )
                    return Response(
                        body=(
                            f"evidence attached: {tgt} --{rel}--> "
                            f"fi{hub_ref_id} (originator split derived at read "
                            f"time — get(id='fi{hub_ref_id}', view='evidence'))"
                        )
                    )
                # rel == 'refines'
                to_hub = authoring.resolve_hub_ref_id(self.store, tgt)
                added = hub.link_claims(
                    self.store,
                    from_hub_ref_id=hub_ref_id,
                    to_hub_ref_id=to_hub,
                    relation=rel,
                    set_by="agent",
                )
                return Response(
                    body=(
                        f"{'linked' if added else 'already linked'}: "
                        f"fi{hub_ref_id} --refines--> fi{to_hub}"
                    )
                )
        # Removing an evidence edge must mirror attach_evidence's direction:
        # the stored edge is paper --role--> hub, so the generic remove
        # (which deletes id→target = hub→paper) would silently match nothing.
        # Intercept HUB_ROLES removals and delete the paper→hub edge directly.
        # (refines removal IS id→target, so it falls through to super() fine.)
        if mode == "remove" and rel in hub.HUB_ROLES:
            try:
                hub_ref_id = authoring.resolve_hub_ref_id(self.store, id)
            except BadInput:
                hub_ref_id = None
            if hub_ref_id is not None:
                if not target or not str(target).strip():
                    raise BadInput(
                        f"link mode='remove' rel={rel!r} on claim hub "
                        f"fi{hub_ref_id} needs the evidence target",
                        next="target='pc<id>' / 'pa<id>' (the supporting paper)",
                    )
                tgt = str(target).strip()
                resolved = self.store.resolve_handle(tgt)
                if resolved is None:
                    raise BadInput(
                        f"evidence target {tgt!r} is not a resolvable paper "
                        "or paper-chunk handle",
                        next="target='pc<id>' / 'pa<id>'",
                    )
                # Match the grounding: attach_evidence stored the edge with
                # src_pos = the paper-chunk ord (a pc<id> target) or NULL (a
                # ref-level pa<id>). resolve_handle carries that same ord as
                # chunk_ord, so passing it here removes the specific grounded
                # edge (remove_link filters src_chunk_id IS NOT DISTINCT FROM).
                n = self.store.remove_link(
                    src_ref_id=resolved.ref_id,
                    dst_ref_id=hub_ref_id,
                    relation=rel,
                    src_pos=getattr(resolved, "chunk_ord", None),
                )
                return Response(
                    body=(
                        f"removed {n} {rel} evidence edge"
                        f"{'' if n == 1 else 's'}: {tgt} ↛ fi{hub_ref_id}"
                    )
                )
        return super().link(id=id, target=target, mode=mode, rel=rel, **_kw)

    # ──────────────────────────────────────────────────────────────────
    # get — intercept view='evidence' (Taproot Phase 2c), else base
    # ──────────────────────────────────────────────────────────────────

    def get(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        view: str | None = None,
        q: str | None = None,
        **_kw: Any,
    ) -> Response:
        """``view='evidence'`` renders a claim hub's evidence, split by
        derived seniority (originators/corroborators/contradicts — see
        :func:`precis.taproot.seniority.derive_evidence`). Every other
        view (bare get, ``links``/``log``/``raw``) falls through to the
        base :class:`~precis.handlers._numeric_ref.NumericRefHandler`.
        Deliberately kept off ``_BASE_VIEWS`` — it's finding-specific,
        not something every numeric-ref kind should expose.
        """
        if view == "evidence":
            ref_id = self._coerce_id(id)
            ref = self._resolve_live_ref(ref_id)
            return self._render_evidence_view(ref)
        return super().get(id=id, view=view, q=q, **_kw)

    def _render_evidence_view(self, ref: Ref) -> Response:
        """Render ``view='evidence'``: the hub's edges by derived role."""
        from precis.format import render_agent_table

        evidence = seniority.derive_evidence(self.store, ref.id)
        all_edges = (
            evidence.originators + evidence.corroborators + evidence.contradictors
        )

        header = [f"# evidence for finding {ref.id}", "", ref.title]
        if not all_edges:
            header.append("")
            header.append("no evidence edges yet for this claim hub")
            return Response(body="\n".join(header))

        def _table(edges: list[seniority.EvidenceEdge]) -> str:
            rows: list[dict[str, str]] = []
            for e in edges:
                paper = e.title[:80] + ("…" if len(e.title) > 80 else "")
                if e.is_originator:
                    paper = f"★ {paper}"
                rows.append(
                    {
                        "paper": paper,
                        "year": str(e.year) if e.year is not None else "—",
                        "support": e.support or "—",
                        "integrity": e.integrity,
                        "caveats": "; ".join(e.caveats) if e.caveats else "—",
                    }
                )
            schema = ["paper", "year", "support", "integrity", "caveats"]
            return render_agent_table(rows, schema=schema)

        lines = list(header)
        lines += ["", "## originators (establishes)", ""]
        lines.append(_table(evidence.originators) if evidence.originators else "(none)")

        lines += ["", "## corroborators", ""]
        lines.append(
            _table(evidence.corroborators) if evidence.corroborators else "(none)"
        )
        if evidence.coverage_note:
            lines += ["", evidence.coverage_note]

        lines += ["", "## contradicts", ""]
        lines.append(
            _table(evidence.contradictors) if evidence.contradictors else "(none)"
        )

        if not any(e.support for e in all_edges):
            lines += ["", "support outcomes are populated by chase (Phase 3)"]

        return Response(body="\n".join(lines))

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
        the ``STATUS:`` closed-vocab tag. Pass ``status='acquiring'`` /
        ``'tracing'`` / ``'multi_candidate'`` / ``'dead_chain'`` to
        inspect each cohort, or ``status='*'`` to see all findings
        regardless.

        The shorthand desugars to ``tags=['STATUS:<value>']`` and
        unions with any explicit ``tags=`` the caller passed, so
        ``search(status='tracing', tags=['topic-co2'])`` works as
        expected.

        **Default cohort (no explicit ``status=``):** ``STATUS:established``
        findings **plus** taproot claim hubs (``TAPROOT:claim`` — minted by
        ``taproot/hub.py::mint_hub`` with ``STATUS:canonical``, off the
        chase-status lifecycle). This is the natural "what evidence do
        we have for X?" shape — the agent rarely wants in-flight rows mixed
        in, but a claim hub is a first-class answer even before its chain
        resolves. The union keys off the ``TAPROOT:claim`` *tag*, so it holds
        whatever status a hub carries. An *explicit* ``status=`` (including
        ``status='established'``) is an exact single-status filter and does
        NOT include hubs unless asked for directly (``status='canonical'`` or
        ``status='*'``).

        Renders results as a TOON table (``id | title | setup |
        primary``) so the agent gets a scannable list — the begat
        chain detail lives behind ``get(kind='finding', id=N)``.
        """
        base_tags: list[str] = list(tags) if tags else []

        if status is None:
            return self._search_default_cohort(
                q=q, base_tags=base_tags, page_size=page_size
            )

        # Explicit status= (including '*') — exact single-status filter,
        # unchanged from before hubs were surfaced in the default cohort.
        effective_tags = base_tags
        resolved_status = status.strip()
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

    def _search_default_cohort(
        self, *, q: str | None, base_tags: list[str], page_size: int
    ) -> Response:
        """The defaulted (``status is None``) search cohort: union of
        ``STATUS:established`` rows and ``TAPROOT:claim`` hubs.

        The store's tag filter (``Tag.normalize_filter`` →
        ``build_tag_filter``) is AND-only over a single tag set — there's
        no any-of/OR group to express "established OR hub" in one query.
        So this runs the two tag-filtered queries separately (each still
        ANDs in any caller-supplied ``tags=``) and unions the results by
        ref id, established first (its natural rank/recency order) then
        any hub not already present — least invasive given the store API,
        and ``page_size`` is honoured by trimming the merged list.
        """
        established_tags = Tag.normalize_filter(
            _tags_with(base_tags, f"{_STATUS_NAMESPACE}:established"), kind=self.kind
        )
        hub_tags = Tag.normalize_filter(
            _tags_with(base_tags, _TAPROOT_CLAIM_TAG), kind=self.kind
        )

        if q is None or not q.strip():
            established_refs = self.store.list_refs(
                kind=self.kind, tags=established_tags, limit=page_size
            )
            hub_refs = self.store.list_refs(
                kind=self.kind, tags=hub_tags, limit=page_size
            )
            refs = _merge_dedup(established_refs, hub_refs)[:page_size]
            return self._render_finding_table(refs, query=None)

        established_hits = self.store.search_refs_lexical(
            q=q, kind=self.kind, tags=established_tags, limit=page_size
        )
        hub_hits = self.store.search_refs_lexical(
            q=q, kind=self.kind, tags=hub_tags, limit=page_size
        )
        refs = _merge_dedup(
            [r for r, _rank in established_hits],
            [r for r, _rank in hub_hits],
        )[:page_size]
        if not refs:
            body = f"no finding matches {q!r} with status='established'"
            from precis.utils.next_block import render_next_section

            nav: list[tuple[str, str]] = [
                (
                    f"search(kind='finding', q={q!r}, status='*')",
                    "drop the status filter",
                ),
                (
                    "search(kind='finding', q='broader term', status='established')",
                    "loosen the query",
                ),
            ]
            body += render_next_section(nav)
            return Response(body=body)

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
        title: str | None = None,
        unacquirable_note: str | None = None,
        dry_run: bool | str | None = None,
        **_kw: Any,
    ) -> Response:
        """Resolve a ``STATUS:multi_candidate`` finding by picking one cite,
        retitle a ``TAPROOT:claim`` hub, or record an author's
        unacquirable-source override. Mutually exclusive kwargs — pass
        exactly one.

        **Pick a candidate.** When the chase reaches a chunk citing
        multiple references (e.g. ``[12,13]``) and can't disambiguate
        automatically, it tags the finding ``STATUS:multi_candidate`` and
        writes one ``derived-from`` link per candidate with
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

        ``title=`` is a **different** operation, only valid on a
        ``TAPROOT:claim`` hub (``id`` must resolve to one — see
        :func:`~precis.taproot.authoring.resolve_hub_ref_id`): it reroutes
        through :func:`precis.taproot.hub.refine_claim_sentence`, the single
        write door that keeps ``refs.title``, the ``finding_body`` chunk,
        and the content-derived ``pub_id`` in sync when a hub's claim
        sentence is reworded (fixing a claim-quality issue, e.g. a dangling
        demonstrative). A plain (non-hub) finding has no ``edit(title=…)``
        door — mutate its claim via a fresh ``put()``.

        **Unacquirable override.** A print-only / undigitized source is
        legitimately citeable even when no digital copy is obtainable.
        Recording that intent suppresses the trust surfaces' "unverified"
        mark on this claim (docs/proposals/finding-trust-surfaces.md;
        never the "unsupported" mark — a negative terminal verification
        always outranks the override, the paper was read):

            edit(kind='finding', id=N, unacquirable_note='print-only 1962 monograph')

        Sets ``meta.unacquirable_override = {by, at, note}``. Settable
        pre-emptively on ANY lifecycle state — not gated to
        ``STATUS:dead_chain(reason=unacquirable)``, since the author may
        know a source is print-only before the chase ever attempts
        acquisition. ``note`` is required (empty/whitespace rejected — a
        silent override defeats the audit purpose); ``at`` is
        server-stamped; ``by`` is ``'agent'`` today (no caller-identity
        channel exists yet for a handler to read one from). Idempotent —
        re-setting just overwrites the prior ``by``/``at``/``note``.

        No op here supports ``dry_run`` (see below).
        """
        given = [
            name
            for name, value in (
                ("pick_candidate", pick_candidate),
                ("title", title),
                ("unacquirable_note", unacquirable_note),
            )
            if value is not None
        ]
        if len(given) > 1:
            raise BadInput(
                "edit(kind='finding') accepts exactly one of pick_candidate, "
                f"title, or unacquirable_note — got {', '.join(given)}",
                next=(
                    "edit(kind='finding', id=<N>, pick_candidate='<cite_key>') / "
                    "edit(kind='finding', id='fi<N>', title='<reworded claim>') / "
                    "edit(kind='finding', id=<N>, unacquirable_note='<why>')"
                ),
            )
        if title is not None:
            if dry_run:
                raise BadInput(
                    "edit(kind='finding', title=…) does not support dry_run — "
                    "the retitle has no preview; omit dry_run to apply",
                    next="edit(kind='finding', id='fi<N>', title='<reworded claim>')",
                )
            return self._retitle_hub(id=id, title=title)
        if dry_run:
            # Neither op has a faithful preview yet: pick_candidate rewrites
            # links + flips status; unacquirable_note writes an audit-trail
            # meta patch. Reject loudly rather than silently apply on
            # dry_run (a data-loss footgun either way).
            raise BadInput(
                "edit(kind='finding') does not support dry_run — it either "
                "promotes a candidate cite (rewrites links + flips status) or "
                "records an unacquirable-source override; omit dry_run to apply",
                next=(
                    "edit(kind='finding', id=<N>, pick_candidate='<cite_key>') or "
                    "edit(kind='finding', id=<N>, unacquirable_note='<why>')"
                ),
            )
        if id is None:
            raise BadInput(
                "edit(kind='finding') requires id=<finding ref_id or pub_id>",
                next=(
                    "edit(kind='finding', id=<N>, pick_candidate='<cite_key>') or "
                    "edit(kind='finding', id=<N>, unacquirable_note='<why>')"
                ),
            )
        if unacquirable_note is not None:
            return self._set_unacquirable_override(id, unacquirable_note)
        if pick_candidate is None or (
            isinstance(pick_candidate, str) and not pick_candidate.strip()
        ):
            raise BadInput(
                "edit(kind='finding') requires pick_candidate=<cite_key or ref_id> "
                "or unacquirable_note=<why source can't be digitally acquired>",
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

    # ──────────────────────────────────────────────────────────────────
    # edit(title=...) — retitle a TAPROOT:claim hub (taproot/hub.py door)
    # ──────────────────────────────────────────────────────────────────

    def _retitle_hub(self, *, id: int | str | None, title: str) -> Response:
        """``edit(kind='finding', title=…)`` — reword a claim hub's sentence.

        ``id`` must resolve to a live ``TAPROOT:claim`` hub (mirrors the
        ``link()`` Taproot-routing check above). A plain finding — no
        ``edit(title=…)`` door exists for it — raises the same sharp
        ``BadInput`` an unresolvable/non-hub target does.
        """
        if id is None:
            raise BadInput(
                "edit(kind='finding', title=…) requires id=<hub ref_id, "
                "fi<id> handle, or pub_id>",
                next="edit(kind='finding', id='fi<N>', title='<reworded claim>')",
            )
        try:
            hub_ref_id = authoring.resolve_hub_ref_id(self.store, id)
        except BadInput:
            hub_ref_id = None
        if hub_ref_id is None:
            raise BadInput(
                f"edit(kind='finding', title=…) only retitles a TAPROOT:claim "
                f"hub — id={id!r} does not resolve to one",
                next=(
                    "a plain (non-hub) finding has no title-edit door — "
                    "record a fresh put(kind='finding', title=…) instead"
                ),
            )
        try:
            result = hub.refine_claim_sentence(
                self.store, hub_ref_id, title, set_by="agent"
            )
        except ValueError as exc:
            raise BadInput(
                f"edit(kind='finding', id='fi{hub_ref_id}', title=…) failed: {exc}",
                next=(
                    "the reworded sentence's pub_id collides with a different "
                    "hub — pick distinct wording, or resolve the dedup by hand "
                    "(link_claims / delete one hub) before retitling"
                ),
            ) from exc
        alias_note = (
            " (old pub_id kept as an alias — existing [handle] cites still resolve)"
            if result["pub_id_alias_kept"]
            else ""
        )
        return Response(
            body=(
                f"retitled claim hub fi{hub_ref_id}\n"
                f"old: {result['old_title']}\n"
                f"new: {result['new_title']}\n"
                f"pub_id: {result['pub_id']}{alias_note}"
            )
        )

    # ──────────────────────────────────────────────────────────────────
    # edit — unacquirable_note (trust-surfaces override write path)
    # ──────────────────────────────────────────────────────────────────

    def _set_unacquirable_override(self, raw_id: int | str, note: str) -> Response:
        """Write ``meta.unacquirable_override`` — the write path behind
        ``edit(kind='finding', unacquirable_note=…)``
        (docs/proposals/finding-trust-surfaces.md). ``note`` required
        non-empty; the override is otherwise settable on any finding
        regardless of its current lifecycle status."""
        if not note.strip():
            raise BadInput(
                "edit(kind='finding') requires a non-empty unacquirable_note "
                "— a silent override defeats the audit purpose",
                next=(
                    "edit(kind='finding', id=<N>, "
                    "unacquirable_note='<why this source cannot be digitally acquired>')"
                ),
            )
        finding_ref_id = self._resolve_finding_ref_id(raw_id)
        override = {
            "by": "agent",
            "at": datetime.now(UTC).isoformat(),
            "note": note.strip(),
        }
        self.store.update_ref(
            finding_ref_id, meta_patch={"unacquirable_override": override}
        )
        return Response(
            body=(
                f"recorded unacquirable override on finding id={finding_ref_id}\n"
                f"note: {override['note']}\n"
                "trust surfaces now render this claim clean (unless a terminal "
                "verification found the source doesn't back it — that always "
                "outranks the override)"
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
        # the ref itself. For a taproot claim hub the body IS the title
        # verbatim (mint_hub writes the sentence to both — refs.title for
        # list-view scannability, the chunk for embedding/search); showing
        # both back reads as accidental duplication, so suppress the
        # ``claim:`` echo when it adds nothing over ``title:``. A plain
        # finding's body carries the setup envelope too, so it differs and
        # still renders.
        body_text = self._fetch_body(ref.id)
        if body_text and body_text.strip() != (ref.title or "").strip():
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

        # Trust-surfaces override (finding-trust-surfaces.md §2): an
        # author's stated reason a print-only / undigitized source can
        # never be acquired, suppressing the export/badge "unverified"
        # mark. Shown whenever set, regardless of current lifecycle status.
        override = meta.get("unacquirable_override")
        if isinstance(override, dict) and override.get("note"):
            lines.append("")
            lines.append(
                f"unacquirable override: {override['note']} "
                f"(by {override.get('by', '?')}, at {override.get('at', '?')})"
            )

        # Acquisition-mode findings (finding-acquisition-mode.md): the
        # DREAM:acquire paper stub(s) this claim is waiting on before the
        # chase worker can ground it. Only present on STATUS:acquiring
        # findings (and any that once were, before flipping to tracing —
        # the link is never removed, only walked past).
        awaits = self.store.links_for(
            ref.id, direction="out", relation=_AWAITS_EVIDENCE
        )
        if awaits:
            lines.append("")
            lines.append("awaiting evidence from:")
            for link in awaits:
                stub = self._fetch_ref_any_kind(link.dst_ref_id)
                handle = handle_registry.try_format(stub.kind, stub.id) or (
                    stub.slug or f"ref:{stub.id}"
                )
                held = (
                    "held" if stub.pdf_sha256 is not None else "stub (awaiting fetch)"
                )
                lines.append(f"  {handle} ({held}) — {stub.title}")

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


def _tags_with(base: list[str], tag: str) -> list[str]:
    """Return ``base`` with ``tag`` appended, unless already present.

    Mirrors the dedup already used for the explicit-status shorthand —
    ``Tag.normalize_filter``/``build_tag_filter`` count *distinct* tags in
    an AND ``HAVING COUNT(...)``, so passing the same tag twice would
    silently require it to match two different tag rows and always miss.
    """
    return base if tag in base else [*base, tag]


def _merge_dedup(primary: list[Ref], secondary: list[Ref]) -> list[Ref]:
    """Union two ref lists by ``id``, keeping ``primary``'s order first
    then any ``secondary`` entries not already present."""
    seen = {r.id for r in primary}
    merged = list(primary)
    for r in secondary:
        if r.id not in seen:
            merged.append(r)
            seen.add(r.id)
    return merged


__all__ = ["FindingHandler"]
