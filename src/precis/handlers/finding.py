"""FindingHandler — chain head over a citation chase to a primary source.

First layer of one domain (:mod:`precis.taproot` = the claim graph built on
findings, :mod:`precis.nanopub` = the publish pipeline that freezes an
approved hub into a signed artifact); this module owns the kind surface
only — verbs, the trust axis, the chase lifecycle. Hub/evidence-edge
mechanics and publish mechanics are not restated here.

A `finding` is the **synthesised endpoint** of a citation chase: the
claim text + its setup context + the chain of `derived-from` links
from the agent's initial citation down to the primary source. It is
the agent's *answer* to "what evidence do we have for X?".

This handler owns the **write door** for findings:

- ``put(title, body, scope, cited_in)`` creates a new finding, the
  ``finding_body`` chunk that holds claim + setup as flowing prose,
  the initial ``derived-from`` link to the cited frontier, and
  tags it ``STATUS:tracing``.
- ``put(hypothesis=True, motivation, testable_by, motivated_by)``
  proposes a nanopub **hypothesis** — a conjecture, which by type
  carries no evidence at all and stands on the artifacts that
  provoked it instead. Prepares and parks it for human approval;
  it never approves. See :mod:`._finding_hypothesis`.
- ``get(id)`` renders the begat-style detail (claim, setup, primary,
  via-chain, status). ``view='evidence'`` (:mod:`._finding_evidence`),
  ``view='nanopub'`` (:mod:`._finding_nanopub`) and
  ``view='mint-preflight'`` (:mod:`._finding_mint_preflight`, the real
  mint gates run read-only) and ``view='judgments'``
  (:mod:`._finding_judgments`, what the refine judges did to a hub's
  edges) are the finding-specific views.
- ``search(q, status=..., trust=...)`` filters by status (default
  ``STATUS:established``) and falls through to the base full-text
  + ANN hybrid; see :meth:`FindingHandler.search`.
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

Module split (docs/backlog/codereview-residuals.md):
``FindingHandler`` still subclasses
:class:`~precis.handlers._numeric_ref.NumericRefHandler` — it genuinely
uses the shared CRUD contract (``tag``/``delete``, ``get``'s view
dispatch + list-view infra, id coercion, live-ref resolution, the
create-time put guard), so graduating it to a standalone handler would
mean reimplementing all of that. What had actually outgrown the base was
the finding-*specific* mass bolted onto it: three large, store-only state
machines that never touch any other handler state. Those moved to
sibling modules so this file stays legible:

* :mod:`precis.handlers._finding_acquire` — the acquisition-mode
  (``wants=``) mint, ``put_acquiring()``.
* :mod:`precis.handlers._finding_edit` — the ``edit()`` verb's ops
  (pick_candidate / title / unacquirable_note / sharpen / body edit).
* :mod:`precis.handlers._finding_evidence` — ``get(view='evidence')``
  rendering + patent-family collapsing.
* :mod:`precis.handlers._finding_judgments` — ``get(view='judgments')``
  plus the compact judged/withheld/removed counts the evidence view and the
  fisheye posture header carry.
* :mod:`precis.handlers._finding_common` — the one helper
  (``fetch_ref_any_kind``) shared across this file and those three.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, ClassVar

from psycopg.errors import UniqueViolation

from precis.errors import BadInput, NotFound, Unsupported
from precis.handlers import (
    _finding_acquire,
    _finding_edit,
    _finding_evidence,
    _finding_judgments,
    _finding_nanopub,
)
from precis.handlers._finding_common import (
    fetch_ref_any_kind,
    multi_article_advisory,
)
from precis.handlers._numeric_ref import NumericRefHandler
from precis.identity import make_finding_paper_id, make_pub_id
from precis.protocol import KindSpec
from precis.response import Response
from precis.store.types import (
    FINDING_TRUST_TIERS,
    ChunkInsert,
    Ref,
    Tag,
)
from precis.taproot import authoring, hub
from precis.taproot.seniority import is_claim_hub
from precis.utils import handle_registry
from precis.utils.glyph_health import glyph_caveat
from precis.utils.link_tag_ops import apply_tag_ops
from precis.utils.link_target import LinkTarget, parse_link_target
from precis.utils.ref_hybrid import fused_ref_hits
from precis.utils.search_merge import SearchHit, ref_hits_to_search_hits
from precis.workers.embed import nudge_fresh_embed

if TYPE_CHECKING:
    from precis.nanopub.overview import HubOverviewRow
    from precis.store.store import Store

log = logging.getLogger(__name__)

_STATUS_NAMESPACE = "STATUS"
_STATUS_TRACING = "tracing"
_DERIVED_FROM = "derived-from"
_AWAITS_EVIDENCE = "awaits-evidence"
# A ``finding`` ref tagged this value is a taproot claim hub (hub mechanics:
# ``taproot/__init__.py``). Hubs carry ``STATUS:canonical``, off the chase
# lifecycle, so ``chase.py::claim_tracing_findings`` never re-claims them.
# The default (no explicit ``status=``) search cohort below unions on this
# *tag*, so a minted hub is visible regardless of its status value, without
# the ``status='*'`` workaround.
_TAPROOT_CLAIM_TAG = "TAPROOT:claim"
# The other half of the claim-hub definition
# (``taproot/canon.py::claim_hub_predicate_sql``; ``mint_hub`` writes both
# tags atomically, and is the sole writer of ``TAPROOT:claim`` anywhere —
# but a demotion can later strip ``STATUS:canonical`` off a hub without
# touching ``TAPROOT:claim``, per that predicate's own docstring).
# :meth:`FindingHandler.search_hits` ANDs this onto :data:`_TAPROOT_CLAIM_TAG`
# so its candidate pool is live-hub-only at the query itself, not just at
# the posture-read backstop.
_STATUS_CANONICAL_TAG = "STATUS:canonical"

# ── the trust axis ────────────────────────────────────────────────────
# Orthogonal to ``STATUS:`` — that is the *chase lifecycle* (how far the
# citation chase got), which says nothing about whether anyone checked the
# claim. ``trust=`` is the epistemic axis: how far up the publish ladder
# a claim hub has actually climbed (state machine: ``nanopub/__init__.py``,
# ``nanopub/state.py``).
_TRUST_ANY = "any"
#: Default-cohort over-fetch: the relevance query runs over the caller's
#: tags alone and is then filtered to established rows + claim hubs, so it
#: fetches this many pages to keep a full page after the filter (gr458942).
_COHORT_OVERFETCH = 4
#: ``trust=`` filters *after* retrieval (the posture lives in publish rows
#: and links, not in the search index), so the retrieval leg has to over-
#: fetch or the filter would just thin an already-truncated page. Factor,
#: not unbounded: a trust query is a narrowing question, and a page that
#: still comes back short is honest about how little is settled.
_TRUST_POOL_FACTOR = 5
_TRUST_POOL_MAX = 100


def _refuse_hand_set_established(
    added: Iterable[Tag], pre_tags: set[Tag], *, where: str
) -> None:
    """Refuse a hand-set ``STATUS:established`` (A3).

    ``established`` is the grounded cohort search defaults to; only the
    chase worker may write it, after it has traced the claim to a
    primary source. A hand tag would surface an ungrounded claim there.
    ``where`` names the door for the message ("finding id=N" on
    ``tag()``, "a new finding" on ``put(tags=...)``).
    """
    for t in added:
        if t.namespace == "closed" and t.prefix == "STATUS":
            if t.value == "established" and t not in pre_tags:
                raise BadInput(
                    f"{where}: STATUS:established is written "
                    "by the chase worker, not by hand",
                    options=["STATUS:tracing", "STATUS:acquiring"],
                    next=(
                        "register the source so the worker chases it: "
                        "get(kind='skill', id='precis-finding-help') "
                        "(put a finding with its provenance/wants); the "
                        "worker sets STATUS:established once grounded"
                    ),
                )


class FindingHandler(NumericRefHandler):
    #: Agent-authored assertions (graph-node-trust-state slice 1).
    trust_origin: ClassVar[str | None] = "generated"
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
        supports_search_hits=True,
        supports_edit=True,
        edit_modes=("replace", "find-replace"),
        supports_delete=True,
        supports_tag=True,
        supports_link=True,
        is_numeric=True,
        id_required=False,
        note_like=False,
    )
    kind: ClassVar[str] = "finding"
    sense: ClassVar[str] = "finding"
    #: ``get``/``tag`` take ``id=[...]`` (gr506770 — reading N findings
    #: cost N calls). ``link`` keeps the single-id door: its Taproot
    #: relations route per-source-hub, so a fan-in batch would have to
    #: resolve which sources are claim hubs anyway.
    batch_ids: ClassVar[bool] = True
    #: ``edit(id=[...])`` runs the handler's own one-transaction batch.
    edit_id_list: ClassVar[str] = "native"

    # ──────────────────────────────────────────────────────────────────
    # put — create a new finding (idempotent on deterministic pub_id)
    # ──────────────────────────────────────────────────────────────────

    def put(
        self,
        *,
        id: str | int | None = None,
        title: str | None = None,
        body: str | None = None,
        scope: dict[str, Any] | None = None,
        cited_in: str | None = None,
        supporters: list[dict[str, Any]] | None = None,
        dedup: bool | str = True,
        wants: list[dict[str, Any]] | None = None,
        provenance: str | None = None,
        hypothesis: bool = False,
        motivation: str | None = None,
        testable_by: str | None = None,
        motivated_by: list[str] | None = None,
        llm_models: list[str] | None = None,
        from_memory: str | None = None,
        meta: dict[str, Any] | None = None,
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

        **Ordinary (chase) mode.** Required: ``title`` (the claim
        sentence — it carries all the meaning and is stored full-length,
        never truncated), ``body`` (claim text + setup envelope as
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

        **Acquisition mode** — the claim's
        supporting paper isn't in the corpus yet: pass ``wants=`` (a list
        of ``{'doi':…}`` / ``{'arxiv':…}`` / ``{'title':…,'url':…}``
        descriptors, ≥1) and ``provenance=`` (a ref/chunk handle for
        where the claim came from) INSTEAD of ``cited_in=``. Mints
        ``STATUS:acquiring`` plus one ``DREAM:acquire`` paper stub per
        descriptor; the chase worker grounds it once a stub lands a PDF.
        See :func:`precis.handlers._finding_acquire.put_acquiring`.

        **Hub mode** — pass ``supporters=`` instead of
        ``cited_in=``/``wants=`` to mint/converge a Taproot claim hub. Runs
        the semantic-dedup cascade first (``taproot/canon.py``: ``block``
        -> ``dedup_judge`` -> ``place``, same as ``taproot/directed.py``'s
        ``directed_mint``): a matched existing claim attaches the
        supporters there instead of minting a near-duplicate hub; a risky/
        unconfirmed match mints anyway and files a review todo naming both
        hubs. ``dedup=False`` skips the cascade and mints unconditionally
        (needs no embedder). A claim that is the same as, a component of,
        or a generalization of a near hub (or a hub already grounded on the
        same passage) is refused with ``options`` attach / refine / force:
        ``dedup='attach:fi<N>'`` attaches the supporters to that hub,
        ``dedup='force'`` mints anyway and stamps ``meta.dedup_override``.
        See :mod:`precis.handlers._finding_hub_mint`.

        **Hypothesis mode** — pass ``hypothesis=True`` with ``motivation=``,
        ``testable_by=``, ``motivated_by=`` (≥2 artifacts across ≥2 source
        papers) and ``llm_models=`` (the authoring model id(s) — an
        agent-prepared artifact attributes its machine author) to propose a
        nanopub hypothesis: a conjecture with no evidence by type, prepared
        and parked for a human to approve. See
        :func:`precis.handlers._finding_hypothesis.put_hypothesis`.

        Existing-id ``put`` is rejected (mutate via tag/link/delete
        per the seven-verb surface).
        """
        # Argument validation — shared with the base / citation handler
        # so mistakes return sharp errors instead of half-created rows.
        self._reject_mutating_put(
            id=id, mode=mode, untags=untags, unlink=unlink, rel=rel, link=link
        )
        # A3 covers the ``tag`` verb; the same refusal applies to the
        # create-time ``tags=`` shortcut — otherwise ``put`` would mint
        # the grounded-cohort marker ``tag`` refuses to write (gr496191).
        if tags:
            _refuse_hand_set_established(
                [Tag.parse_strict(t, kind=self.kind) for t in tags],
                pre_tags=set(),
                where="a new finding",
            )

        body_text = body if body is not None else text

        # ``meta=`` is the machine-writer admission annotation
        # (``meta.{model,prompt_version,run_id,why}`` — the shape
        # ``link(kind='finding', meta=...)`` already carries to the edge,
        # gr504789/gr506767). It merges into the minted ref's meta under
        # the handler's own keys; on a dedup *converge* (hub mode) no row
        # is minted so it is dropped, same rule as mint_hub's extra_meta.
        if meta is not None and not isinstance(meta, dict):
            raise BadInput(
                f"meta= must be a dict, got {type(meta).__name__}",
                next="meta={'model': '<model id>', 'run_id': '<run>', 'why': '...'}",
            )

        # `report` / `definition` are approve-only (nanopub/gates.py): a
        # mint-time `artifact_type=` would otherwise vanish into **_kw and
        # leave the caller believing the type was set.
        if _kw.get("artifact_type") is not None:
            raise BadInput(
                f"artifact_type={_kw['artifact_type']!r} cannot be set at put — "
                "the report/definition artifact types are approve-only",
                next=(
                    "put the finding as an ordinary claim; a human sets "
                    "artifact_type on the nanopub approve payload "
                    "(web /nanopub review or `precis nanopub approve`)"
                ),
            )

        # --- Hypothesis-proposal mode ---
        # A finding born with ``hypothesis=True`` is a conjecture, not a
        # finding: it has no evidence by type (the mint gates reject a
        # hypothesis that arrives with grounding passages) and stands on
        # ``motivated_by=`` artifacts instead. Deliberately checked before
        # every other mode so a caller mixing it with supporters=/cited_in=
        # gets a mode-conflict error rather than a confusing missing-field
        # one. See :mod:`precis.handlers._finding_hypothesis`.
        if hypothesis:
            if cited_in is not None or supporters is not None or wants is not None:
                raise BadInput(
                    "hypothesis mode is exclusive — a conjecture has no "
                    "evidence by definition, so it takes motivated_by= "
                    "rather than supporters=/cited_in=/wants=.",
                    next=(
                        "put(kind='finding', hypothesis=True, title=<sentence>, "
                        "motivation=..., testable_by=..., "
                        "motivated_by=['pc293','fi1234'])"
                    ),
                )
            from precis.handlers import _finding_hypothesis

            return _finding_hypothesis.put_hypothesis(
                self.store,
                sentence=body_text or title or "",
                scope=scope,
                motivation=motivation,
                testable_by=testable_by,
                motivated_by=motivated_by,
                llm_models=llm_models,
                from_memory=from_memory,
                meta=meta,
            )

        # --- Taproot hub-mint mode ---
        # A finding born with paper ``supporters=`` (and no ``cited_in``) is a
        # claim HUB, not a chase target: route through
        # :mod:`precis.handlers._finding_hub_mint`, which runs the semantic-
        # dedup cascade (``taproot/canon.py``: block -> dedup_judge -> place)
        # then the single write door (``taproot/hub.py``/``taproot/
        # authoring.py``) — mints/converges the hub (or attaches onto a
        # matched existing one) and attaches each supporter's
        # ``paper --role--> hub`` evidence edge. The grounding invariant
        # holds by construction — every path REQUIRES paper supporters, so
        # this door can never mint a thin-air hub. ``title=``/``body=``
        # carry the canonical claim sentence.
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
            sentence = (title or body_text or "").strip()
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
            from precis.handlers import _finding_hub_mint

            return _finding_hub_mint.put_hub(
                self.store,
                sentence=sentence,
                scope=scope or {},
                supporters=supporters,
                set_by="agent",
                dedup=dedup,
                embedder=getattr(self.hub, "embedder", None),
                extra_meta=meta,
            )

        # --- Acquisition mode (claim-first mint) ---
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
            return _finding_acquire.put_acquiring(
                self.store,
                kind=self.kind,
                title=title,
                body_text=body_text,
                scope=scope,
                wants=wants,
                provenance=provenance,
                parent_id=parent_id,
                tags=tags,
                link=link,
                rel=rel,
                collision_response=self._collision_response,
                meta=meta,
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
                # gr518635: agents retried the identical put 8x on the
                # prose hint — lead with a copyable call carrying the
                # caller's own claim as the probe.
                probe = " ".join(str(title or "").split()[:10])[:80]
                next_hint: str | list[str] = [
                    (
                        "a finding MUST cite a corpus chunk — do NOT resubmit "
                        "the same put without cited_in. Find the source's "
                        "handle first: "
                        + (
                            f"search(kind='paper', q={probe!r})"
                            if probe
                            else "search(kind='paper', q='<words from the source title>')"
                        )
                        + " — then cite a handle from its hits: "
                        "cited_in='pc42' (chunk) or 'miller23a' (ref-level)"
                    ),
                    (
                        "source not in the corpus yet? "
                        "put(kind='paper', doi='10.1/x') or "
                        "put(kind='paper', arxiv='2401.00001') mints the "
                        "stub — cite the resulting handle once ingested"
                    ),
                    (
                        "a canonical cross-paper claim mints a hub instead: "
                        "put(kind='finding', title=<claim>, supporters="
                        "[{'paper':'pa5','source_handle':'pc293'}]). Your "
                        "own synthesis with no single source is not a "
                        "finding — write it into a draft or a memory"
                    ),
                ]
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
        title_clean = title.strip()

        ref_meta: dict[str, Any] = {
            **(meta or {}),
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
        facts_deferred = 0
        try:
            with self.store.tx() as conn:
                ref = self.store.insert_ref(
                    kind=self.kind,
                    slug=None,
                    title=title_clean,
                    meta=ref_meta,
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
                self.store.chunks.insert_chunks(
                    ref.id,
                    [
                        ChunkInsert(
                            ord=0,
                            text=body_clean,
                            meta={"chunk_kind": "finding_body"},
                        )
                    ],
                    conn=conn,
                )
                # Embed the body within seconds so a paraphrase search
                # (and the next mint's dedup) can find it.
                nudge_fresh_embed(self.store, conn=conn, reason="put finding")
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
                # Mentions in the claim prose (handles, C-numbers → lazy
                # part refs) become related-to edges — the same write-time
                # autolinker the other note-like kinds run (gr520243).
                facts_deferred = self._sync_mention_links(
                    ref.id, title_clean + "\n" + body_clean, conn=conn
                )
        except UniqueViolation:
            # Collision on pub_id: this finding already exists.
            # Look up the existing ref_id and return it so the
            # caller sees a deterministic "exists" result.
            return self._collision_response(pub_id)
        if extra_target is not None and extra_target.kind == "part":
            self._enqueue_part_datasheet(extra_target.ref_id)

        return self._facts_note(
            Response(
                body=(
                    f"created finding id={ref.id} pub_id={pub_id}\n"
                    f"title: {title_clean}\n"
                    f"frontier: {target.raw}\n"
                    f"status: STATUS:{_STATUS_TRACING}\n"
                    f"placeholder: [{pub_id}] (use in text; precis resolve "
                    f"substitutes the primary cite_key once STATUS:established)"
                )
            ),
            facts_deferred,
        )

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
        meta: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> Response:
        """Taproot-aware link door for findings.

        When the source resolves to a ``TAPROOT:claim`` hub and ``rel`` is a
        Taproot relation, route through the single write door
        (``taproot/hub.py``) rather than a raw ``add_link`` — taproot evidence relations: a raw
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
        - ``rel='explains'`` → this hub is the mechanism/cause that explains
          the ``target`` hub (``fi<id>``); advisory, directed explaining →
          explained, no cycles, no evidence flows.

        ``meta`` (edge metadata, add-only) rides into whichever door
        writes the edge — merged into the evidence edge's own meta, or
        passed through to ``link_claims`` / :meth:`_link` — so a machine
        writer's admission annotation (``meta.evidence`` chunk pins,
        ``meta.{model,prompt_version,run_id,why}``) reaches the gate at
        ``store.add_link`` (gr504789).

        Anything else — a non-hub finding, a non-Taproot relation, or
        ``mode='remove'`` — falls through to the generic numeric-ref link.
        """
        if isinstance(id, list):
            # finding takes batch id=[...] on get/tag, but not link: a
            # fan-in would bypass the per-source Taproot door below
            # (``_link_batch`` calls ``_link`` directly, skipping the
            # claim-hub role guards and ``meta=`` edge payload).
            raise BadInput(
                "link(kind='finding') takes one source id per call — "
                "Taproot relations route per hub",
                next=(
                    "one call per source: link(kind='finding', id=<id>, "
                    "target=..., rel=...); get and tag do take id=[...]"
                ),
            )
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
                            else f"target='fi<id>' (the claim hub {rel} points at)"
                        ),
                    )
                tgt = str(target).strip()
                if rel in hub.HUB_ROLES:
                    if rel == "contradicts":
                        # Same door policy as the generic link()s
                        # (docs/backlog/disputes-edge-nonblocking-
                        # disagreement.md D1-D4): claim-graph
                        # 'contradicts' is adjudication-derived only —
                        # this agent-facing door can't file it, even
                        # though 'contradicts' stays in HUB_ROLES and
                        # attach_evidence for Part 2's programmatic use.
                        # A paper-passage-vs-claim conflict is filed as
                        # a 'disputes' link instead.
                        raise BadInput(
                            "claim-graph 'contradicts' is adjudication-derived "
                            "and cannot be filed manually",
                            next=(
                                "file rel='disputes' instead — free to file, "
                                "non-blocking"
                            ),
                        )
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
                        meta={**(meta or {}), "source_handle": tgt},
                        set_by="agent",
                    )
                    return Response(
                        body=(
                            f"evidence attached: {tgt} --{rel}--> "
                            f"fi{hub_ref_id} (originator split derived at read "
                            f"time — get(id='fi{hub_ref_id}', view='evidence'))"
                            + multi_article_advisory(self.store, resolved.ref_id, tgt)
                        )
                    )
                # rel is a claim-link relation ('refines', 'explains', ...)
                to_hub = authoring.resolve_hub_ref_id(self.store, tgt)
                added = hub.link_claims(
                    self.store,
                    from_hub_ref_id=hub_ref_id,
                    to_hub_ref_id=to_hub,
                    relation=rel,
                    set_by="agent",
                    meta=meta,
                )
                return Response(
                    body=(
                        f"{'linked' if added else 'already linked'}: "
                        f"fi{hub_ref_id} --{rel}--> fi{to_hub}"
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
        # The generic single-id door — ``self._link`` directly, not
        # ``super().link``, so ``meta`` reaches ``add_link`` (the base
        # ``link`` signature has no ``meta`` slot; a list id already
        # raised at ``_coerce_ids`` above, so the batch door never
        # applies here).
        return self._link(id=id, target=target, mode=mode, rel=rel, meta=meta)

    # ──────────────────────────────────────────────────────────────────
    # get — intercept view='evidence' (Taproot Phase 2c), else base
    # ──────────────────────────────────────────────────────────────────

    def get(
        self,
        *,
        id: str | int | list[str | int] | None = None,
        view: str | None = None,
        q: str | None = None,
        # ``view='mint-preflight'``'s candidate payload, arriving through
        # ``get``'s ``args=`` extras channel. Explicit rather than swallowed
        # by ``**_kw``: ``_accepted_kwargs`` deliberately excludes VAR_KEYWORD
        # catch-alls, so an extras key absent from the signature is rejected
        # as a typo before it ever reaches the handler.
        payload: dict[str, Any] | None = None,
        # ``view='merge-plan'``'s loser hub, same channel
        # (``args={'loser': 'fi<N>' | <int> | '<pub_id>'}``).
        loser: str | int | None = None,
        # ``view='judgments'``'s 1-based page, same channel
        # (``args={'page': 2}``).
        page: int | None = None,
        # ``view='judgments'``'s row cap under the canonical slice name
        # (``args={'page_size': 25}``; gr514469).
        page_size: int | None = None,
        # ``view='diff'``'s sha (``args={'since': <sha>}``) — forwarded to
        # the base dispatch; declared so the extras gate accepts it here.
        since: str | int | None = None,
        **_kw: Any,
    ) -> Response:
        """``view='evidence'`` renders a claim hub's evidence, split by
        derived seniority (originators/corroborators/contradicts — see
        :func:`precis.taproot.seniority.derive_evidence`).
        ``view='judgments'`` lists what the refine judges did to the hub's
        edges (``meta.reground_log``, newest first, 50 a page via
        ``args={'page': N}``): action, verdict, source, same_setup/primary/
        terminal, reason, both setup texts, the removed link's stored meta.
        ``view='nanopub'`` renders the hub as TriG — the exact frozen
        artifact bytes once signed, an unsigned draft otherwise
        (:mod:`precis.handlers._finding_nanopub`, nanopub slice 1).
        ``view='mint-preflight'`` runs the real
        :func:`precis.nanopub.gates.run_mint_gates` against a candidate
        payload and returns the violation list — read-only, no state
        change (:mod:`precis.handlers._finding_mint_preflight`). Pass the
        payload through ``get``'s extras channel,
        ``args={'payload': {...}}``; omitted, it gates whatever is already
        frozen or parked on the hub.
        ``view='similar'`` lists the ``k`` nearest OTHER live claim hubs to
        this hub's sentence (:func:`precis.taproot.canon.nearest_hubs`,
        the same ANN retrieval the hub-mint dedup cascade runs before
        minting — :mod:`precis.handlers._finding_hub_mint`), with
        distance and claim text; excludes this hub itself.
        ``view='merge-plan'`` is the read-only dry run of collapsing a
        duplicate hub into this one: ``id`` is the WINNER, the loser comes
        via ``args={'loser': 'fi<N>' | <int> | '<pub_id>'}``
        (:func:`precis.taproot.hub.merge_hubs` with ``dry_run=True``,
        always — :mod:`precis.handlers._finding_merge`). Applying a merge
        is a human door (the claim page's merge form), never an agent verb.
        ``view ∈ kwd|summary|verbatim|fisheye|fisheye+1hop`` (the
        :class:`~precis.workers.working_set.Extent` ladder) renders the
        finding as an eye (:func:`precis.utils.eye_render.render_eye`); at
        ``fisheye+1hop`` a claim hub also grows its claim-graph
        neighborhood one edge out (``establishes``/``corroborates``/
        ``contradicts``/``refines``/``conjunct-of``/``motivated-by`` —
        :data:`precis.utils.refeye.RING_RELATIONS`), and every extent
        carries a leading claim-hub posture header (see
        :func:`_hub_eye_header`).
        Every other view (bare get, ``links``/``log``/``raw``) falls
        through to the base
        :class:`~precis.handlers._numeric_ref.NumericRefHandler`.
        These views are deliberately kept off ``_BASE_VIEWS`` — finding-specific,
        not something every numeric-ref kind should expose.
        """
        if (r := self._route_find(id=id, view=view, q=q)) is not None:
            return r
        if isinstance(id, list):
            # batch_ids: the base _get_batch / _get_batch_view path;
            # finding-only views (evidence, judgments, …) map per id.
            return super().get(id=id, view=view, q=q, since=since, **_kw)
        id = self._resolve_pub_id_slug(id)
        if view == "evidence":
            ref_id = self._coerce_id(id)
            ref = self._resolve_live_ref(ref_id)
            return _finding_evidence.render_evidence_view(self.store, ref)
        if view == "judgments":
            ref_id = self._coerce_id(id)
            ref = self._resolve_live_ref(ref_id)
            return _finding_judgments.render_judgments_view(
                self.store, ref, page=page, page_size=page_size
            )
        if view == "nanopub":
            ref_id = self._coerce_id(id)
            ref = self._resolve_live_ref(ref_id)
            return _finding_nanopub.render_nanopub_view(self.store, ref)
        if view == "mint-preflight":
            from precis.handlers import _finding_mint_preflight

            ref_id = self._coerce_id(id)
            ref = self._resolve_live_ref(ref_id)
            return _finding_mint_preflight.render_mint_preflight(
                self.store, ref, payload=payload
            )
        if view == "similar":
            from precis.handlers import _finding_hub_mint

            ref_id = self._coerce_id(id)
            ref = self._resolve_live_ref(ref_id)
            return _finding_hub_mint.render_similar_view(
                self.store, ref, embedder=getattr(self.hub, "embedder", None)
            )
        if view == "merge-plan":
            from precis.handlers import _finding_merge

            ref_id = self._coerce_id(id)
            ref = self._resolve_live_ref(ref_id)
            return _finding_merge.render_merge_plan_view(self.store, int(ref.id), loser)
        return super().get(id=id, view=view, q=q, since=since, **_kw)

    def eye(
        self,
        *,
        id: Any = None,
        extent: Any = "fisheye",
        q: str | None = None,
        ring: Any = None,
    ) -> Response:
        """The eye ladder on a finding: the shared render led by the
        claim-hub posture header (:func:`_hub_eye_header`); a ``pub_id``
        slug resolves like it does on ``get``."""
        ref = self._resolve_live_ref(self._coerce_id(self._resolve_pub_id_slug(id)))
        resp = super().eye(id=int(ref.id), extent=extent, q=q, ring=ring)
        header = _hub_eye_header(self.store, ref)
        # gr515633: the categorizer hint line, but only once the operator
        # opted the kind in (``categorize_kind:finding`` — findings are
        # default-OFF in ``workers/_claim.py``'s OPT_IN_KINDS).
        from precis.categorizers import ref_hint_line

        hint = ref_hint_line(self.store, ref)
        if hint:
            header += hint + "\n\n"
        return Response(body=header + resp.body)

    def _find_resolve_ref(self, id: Any) -> Ref:
        """pub_id slug resolves like it does on ``get``."""
        return self._resolve_live_ref(self._coerce_id(self._resolve_pub_id_slug(id)))

    def _resolve_pub_id_slug(self, id: str | int | None) -> str | int | None:
        """Translate a bare ``pub_id`` slug (e.g. ``'tbx2hd'``) to its ref_id.

        A draft cites a finding/claim-hub via its ``[<pub_id>]`` placeholder
        token (gr178763 item 1) — the base
        :meth:`~precis.handlers._numeric_ref.NumericRefHandler._coerce_id`
        only accepts an integer id (or the ``'finding:<int>'`` link-target
        form), so an agent holding a pub_id copied out of a citation had no
        way back to the finding it names. Anything already numeric, a
        ``self.kind``-prefixed link target, or a list-view path (``'/recent'``)
        is left untouched; everything else is tried as a pub_id via
        :func:`precis.taproot.authoring.find_hub_by_pub_id` — a plain
        ``ref_identifiers`` reverse lookup, not hub-specific despite living
        in ``taproot/authoring.py``: every finding (hub or plain chase row)
        gets a ``pub_id`` row at ``put()`` (see this module's docstring).
        Caveat: the pub_id alphabet is base32 ``[a-z2-7]``, so an all-digit
        pub_id (every char in ``2-7``, ~1/112k per mint) is shadowed by the
        numeric-id fast path and can't be resolved by slug — accepted rarity;
        such a value still resolves via ``get(kind='finding', id=<ref_id>)``.

        Raises:
            NotFound: ``id`` is a non-numeric string that doesn't match any
                finding's pub_id.
        """
        if not isinstance(id, str):
            return id
        s = id.strip()
        if not s or s.startswith("/"):
            return id
        prefix = f"{self.kind}:"
        body = s[len(prefix) :] if s.startswith(prefix) else s
        if body.lstrip("-").isdigit():
            return id
        ref_id = authoring.find_hub_by_pub_id(self.store, body)
        if ref_id is None:
            raise NotFound(
                f"no finding with pub_id={s!r}",
                next=(
                    "pub_id is minted by put(kind='finding', ...) (chase or "
                    "hub mode) - check for a typo, or "
                    "search(kind='finding', q='...') if you don't have the "
                    "exact token"
                ),
            )
        return ref_id

    # ──────────────────────────────────────────────────────────────────
    # search — status-filtered TOON table
    # ──────────────────────────────────────────────────────────────────

    def _hybrid_hits(
        self,
        *,
        q: str,
        tags: list[str] | None,
        limit: int,
        mode: str | None,
    ) -> list[Ref]:
        """Ref hits for ``q``, fusing the title leg with the claim-body leg.

        Scoped to ``finding_body`` — a hub's claim sentence lives there
        (``ord=0``), and it is the chunk the embedder fills, so this is what a
        "do we already have a claim like this?" query must match. The
        ``card_combined`` cards on chase-tree rows are chain snapshots, not
        claims, and would only add noise.
        """
        return fused_ref_hits(
            self.store,
            getattr(self.hub, "embedder", None),
            q=q,
            kind=self.kind,
            tags=tags,
            limit=limit,
            mode=mode,
            chunk_kinds=["finding_body"],
        )

    def _postures(
        self, ref_ids: list[int], *, strict: bool = False
    ) -> dict[int, HubOverviewRow]:
        """Publish/evidence posture for whichever hits are claim hubs.

        One bounded query (``nanopub.overview.hub_rows(ref_ids=…)``) per
        result page. Non-hub hits simply have no row.

        ``strict`` decides what a failed read means, and the two callers
        genuinely differ. For the **columns** it is signage: a posture we
        couldn't read renders blank, which is degraded but honest, and a
        transient DB blip must never break a search. For the **filter**
        the same empty dict would be a lie — ``passes_trust(None, …)`` is
        ``False`` for every tier, so a swallowed error would empty the
        page and render as "nothing is settled", which is exactly the
        answer someone deciding what to cite must not be handed. So the
        filter passes ``strict=True`` and lets the error surface.
        """
        if not ref_ids:
            return {}
        from precis.nanopub.overview import hub_rows

        try:
            return {row.ref_id: row for row in hub_rows(self.store, ref_ids=ref_ids)}
        except Exception:
            if strict:
                raise
            log.warning("finding search: posture read failed", exc_info=True)
            return {}

    def _cohort_members(self, ref_ids: list[int]) -> set[int]:
        """The ids among ``ref_ids`` that belong to the default cohort —
        ``STATUS:established`` rows or ``TAPROOT:claim`` hubs — read in one
        ``ref_tags_bulk`` query, so a relevance-ranked over-fetch can be
        filtered without a second ranked query per bucket."""
        if not ref_ids:
            return set()
        hub_ns, hub_val = _TAPROOT_CLAIM_TAG.split(":", 1)
        wanted = {(_STATUS_NAMESPACE, "established"), (hub_ns, hub_val)}
        return {
            rid
            for rid, pairs in self.store.ref_tags_bulk(ref_ids).items()
            if wanted.intersection(pairs)
        }

    def _trust_pool(self, page_size: int, trust: str | None) -> int:
        """Retrieval limit for a page — widened when ``trust=`` will cull."""
        if not trust or trust == _TRUST_ANY:
            return page_size
        return min(page_size * _TRUST_POOL_FACTOR, _TRUST_POOL_MAX)

    def _apply_trust(
        self, refs: list[Ref], *, trust: str | None, page_size: int
    ) -> list[Ref]:
        """Cull an over-fetched pool to the requested trust axis, then trim.

        Order is untouched: relevance stays the ranking signal, exactly as
        ``precis-search-help`` promises. ``trust=`` removes rows, it never
        re-ranks them — a settled-but-barely-relevant claim outranking a
        dead-on candidate would be a worse answer, not a safer one.
        """
        if not trust or trust == _TRUST_ANY:
            return refs[:page_size]
        from precis.nanopub.overview import passes_trust

        # strict=True: an unreadable posture must not masquerade as "not
        # settled" — see :meth:`_postures`.
        postures = self._postures([int(r.id) for r in refs], strict=True)
        kept = [r for r in refs if passes_trust(postures.get(int(r.id)), trust)]
        return kept[:page_size]

    # ──────────────────────────────────────────────────────────────────
    # search_hits — cross-kind merge stream: live claim hubs only
    # ──────────────────────────────────────────────────────────────────

    def _guard_tag_add(
        self, ref_id: int, added: list[Tag], pre_tags: set[Tag]
    ) -> list[Tag]:
        """Refuse a hand-set ``STATUS:established`` on the ``tag`` verb."""
        _refuse_hand_set_established(added, pre_tags, where=f"finding id={ref_id}")
        return added

    def search_hits(  # type: ignore[override]
        self,
        *,
        q: str,
        tags: list[str] | None = None,
        page_size: int = 10,
        mode: str | None = None,
        # dispatch-injected — see the identically-named kwargs on
        # ``NumericRefHandler.search_hits``. Declared explicitly (not
        # swallowed by ``**_kw``) so ``uncited=``/``cited=`` keep working
        # now that this kind reaches the cross-kind fan-out.
        exclude_ref_ids: list[int] | None = None,
        include_ref_ids: list[int] | None = None,
        query_vec: list[float] | None = None,
        **_kw: Any,
    ) -> list[SearchHit]:
        """Claim-hub-only cross-kind stream, posture-annotated.

        the claim-layer-in-cross-kind-search design (shipped 2026-09-19) option
        1: naively flipping ``supports_search_hits`` would have handed
        the cross-kind merge the inherited
        ``NumericRefHandler.search_hits`` — every finding (chase-tree
        rows included), each stripped of the ``state``/``support``/
        ``flags`` columns that make a claim hit legible. This override
        IS what the flag flip actually turns on: candidates restricted
        to live claim hubs (``TAPROOT:claim`` + ``STATUS:canonical`` —
        :data:`_STATUS_CANONICAL_TAG`'s docstring), each carrying a terse
        ``posture`` token (:func:`_search_hit_posture`) instead of the
        three-column TOON shape, so the settled-or-not signal rides
        along the merged row rather than being flattened away.

        Posture comes from one batched
        :func:`~precis.nanopub.overview.hub_rows` call per page — the
        same query :meth:`_render_finding_table`'s posture columns use —
        which doubles as a correctness backstop: a tag-filtered candidate
        ``hub_rows`` doesn't return for (rare — ``mint_hub`` is the sole
        ``STATUS:canonical`` writer) is dropped rather than rendered with
        a posture it can't have. ``strict=True`` (see :meth:`_postures`):
        an unreadable posture must raise (surfacing as this kind
        "errored, omitted from this merge" at the dispatch layer — see
        ``runtime.search._cross_kind_invoke_search_hits``), not silently
        degrade to "no hubs this page".
        """
        if not (q and q.strip()):
            return []
        hub_tags = Tag.normalize_filter(
            _tags_with(
                _tags_with(list(tags) if tags else [], _TAPROOT_CLAIM_TAG),
                _STATUS_CANONICAL_TAG,
            ),
            kind=self.kind,
        )
        refs = fused_ref_hits(
            self.store,
            getattr(self.hub, "embedder", None),
            q=q,
            kind=self.kind,
            tags=hub_tags,
            limit=page_size,
            mode=mode,
            chunk_kinds=["finding_body"],
            exclude_ref_ids=exclude_ref_ids,
            include_ref_ids=include_ref_ids,
            query_vec=query_vec,
        )
        if not refs:
            return []
        postures = self._postures([int(r.id) for r in refs], strict=True)
        kept = [r for r in refs if int(r.id) in postures]
        if not kept:
            return []
        pairs: list[tuple[Any, float]] = [
            (r, float(len(kept) - i)) for i, r in enumerate(kept)
        ]
        return ref_hits_to_search_hits(
            pairs,
            kind=self.kind,
            posture_for=lambda ref: _search_hit_posture(postures[int(ref.id)]),
        )

    def search(
        self,
        *,
        q: str | None = None,
        status: str | None = None,
        trust: str | None = None,
        tags: list[str] | None = None,
        page_size: int = 10,
        mode: str | None = None,
        # ``view='count'`` (gr518170): the filtered finding total plus
        # the live claim-graph census — hub totals by publish state and
        # trust axis, evidence/claim/lineage edge counts by relation —
        # so "1,552 hubs and 2,059 edges" is one call, not prod-psql.
        # ``group_by=`` facets the ref total (same grammar as the other
        # numeric-ref kinds); ``since=``/``until=`` bound
        # ``refs.created_at`` on it.
        view: str | None = None,
        group_by: str | list[str] | None = None,
        since: str | None = None,
        until: str | None = None,
        **_kw: Any,
    ) -> Response:
        """Hybrid search across findings with a status-axis default.

        Fuses ``refs.title`` lexical + hybrid (lexical+semantic) over
        ``finding_body`` + a notation-canonicalized leg
        (:mod:`precis.utils.ref_hybrid`). ``mode='lexical'``/``'verbatim'``
        skip the embed.

        ``status=`` filters on the ``STATUS:`` closed-vocab tag
        (``'acquiring'``/``'tracing'``/``'multi_candidate'``/
        ``'dead_chain'``, ``'*'`` = all); desugars to
        ``tags=['STATUS:<value>']``, unioned with any explicit ``tags=``.
        ``status='refuted'`` reaches the do-not-repropose ledger
        (``docs/backlog/quest-dossier-dialectic.md`` §Refuted lifecycle) —
        fenced out of the default cohort and every cross-kind search
        (``store/_tag_filter.py::refuted_fence``), same as
        ``DREAM:speculative``/``ORIGIN:wikipedia``; the ``STATUS:refuted``
        desugar is the opt-in that lifts the fence. ``status='*'`` does
        NOT add that tag, so a refuted finding can still surface only via
        the unfenced title-lexical leg.

        **Default cohort (no ``status=``):** ``STATUS:established``
        findings plus taproot claim hubs (``TAPROOT:claim``, keyed off the
        tag regardless of the hub's own status — hub mechanics:
        ``taproot/__init__.py``). An explicit ``status=`` (including
        ``'established'``) is an exact filter and excludes hubs unless
        asked for directly.

        ``trust=`` is the orthogonal trust axis: not chase progress
        (``status=``) but how far up the publish ladder the claim climbed
        (state machine: ``nanopub/__init__.py``, ``nanopub/state.py``).

        =================== ====================================================
        ``trust=``          keeps a hit when
        =================== ====================================================
        ``'signed'``        publish row is ``signed``/``anchored``/
                            ``published`` — a human attesting key stands
                            behind it
        ``'verified'``      >=1 edge with an *affirmative* verdict (never
                            merely present — a ``corroborates`` edge can
                            verify ``no``) and no live ``contradicts`` edge
        ``'disputed'``      a live ``contradicts`` edge — the inspection
                            cohort, deliberately reachable
        ``'any'`` / omitted no filter
        =================== ====================================================

        A hit with no publish posture fails every tier but ``'any'``.
        Filter runs post-retrieval (over-fetch: :data:`_TRUST_POOL_FACTOR`)
        so the page can come back short when little is settled; ordering
        (relevance) is untouched.

        ``view='count'`` short-circuits to :meth:`_finding_count_view`:
        the filtered finding total (``status=``/``tags=``/``q=``/
        ``since=``/``until=``/``group_by=``, same contract as the other
        numeric-ref kinds) plus the live claim-graph census — hubs by
        publish state and trust axis, edges by relation (gr518170).

        Renders a TOON table (``id | title | setup | primary``, plus
        ``state | support | flags`` once any hit is a claim hub);
        ``get(kind='finding', id=N)`` for the full begat chain.
        """
        base_tags: list[str] = list(tags) if tags else []
        resolved_trust = (trust or "").strip().lower() or None
        if resolved_trust is not None and resolved_trust not in FINDING_TRUST_TIERS:
            raise BadInput(
                f"unknown trust={trust!r}",
                options=list(FINDING_TRUST_TIERS),
                next=(
                    "search(kind='finding', q='…', trust='signed') for claims "
                    "a human key stands behind"
                ),
            )

        if view is not None:
            if view != "count":
                raise BadInput(
                    f"unknown view {view!r} for search(kind={self.kind!r})",
                    options=["count"],
                    next=(
                        f"search(kind={self.kind!r}, view='count', "
                        "args={'group_by': 'state'})"
                    ),
                )
            return self._finding_count_view(
                q=q,
                status=status,
                trust=resolved_trust,
                tags=base_tags,
                group_by=group_by,
                since=since,
                until=until,
            )

        if status is None:
            return self._search_default_cohort(
                q=q,
                base_tags=base_tags,
                page_size=page_size,
                mode=mode,
                trust=resolved_trust,
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
                    kind=self.kind,
                    tags=normalized,
                    limit=self._trust_pool(page_size, resolved_trust),
                )
                refs = self._apply_trust(
                    refs, trust=resolved_trust, page_size=page_size
                )
                return self._render_finding_table(refs, query=None)
            raise BadInput(
                "search(kind='finding') requires q= or status=/tags=",
                next=(
                    "search(kind='finding', q='2.4 kV gate dielectric') or "
                    "search(kind='finding', status='tracing')"
                ),
            )

        refs = self._hybrid_hits(
            q=q,
            tags=normalized,
            limit=self._trust_pool(page_size, resolved_trust),
            mode=mode,
        )
        refs = self._apply_trust(refs, trust=resolved_trust, page_size=page_size)
        if not refs:
            tag_suffix = (
                f" with status={resolved_status!r}" if resolved_status != "*" else ""
            )
            if resolved_trust:
                tag_suffix += f" at trust={resolved_trust!r}"
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
            if resolved_trust:
                nav.insert(
                    0,
                    (
                        f"search(kind='finding', q={q!r}, status={resolved_status!r})",
                        "drop the trust filter",
                    ),
                )
            body += render_next_section(nav)
            return Response(body=body)

        return self._render_finding_table(refs, query=q)

    def _search_default_cohort(
        self,
        *,
        q: str | None,
        base_tags: list[str],
        page_size: int,
        mode: str | None = None,
        trust: str | None = None,
    ) -> Response:
        """The defaulted (``status is None``) search cohort: union of
        ``STATUS:established`` rows and ``TAPROOT:claim`` hubs.

        The store's tag filter (``Tag.normalize_filter`` →
        ``build_tag_filter``) is AND-only over a single tag set — there's
        no any-of/OR group to express "established OR hub" in one query.

        **No q (recency):** the two tag-filtered lists are unioned by ref
        id, established first then any hub not already present — there is
        no cross-bucket score to fuse, so bucket order is the honest one.

        **With q (relevance):** one relevance-ranked query over the
        caller's ``tags=`` alone (over-fetched ``_COHORT_OVERFETCH`` ×),
        then keep the rows that carry either cohort tag, in that rank
        order. Two separately-capped bucket queries appended established-
        first were the gr458942 defect: the semantic leg never runs dry,
        so the established bucket's ten least-bad neighbours filled the
        page and a hub that matched the claim sentence near-verbatim was
        trimmed off unseen. Only when the over-fetch leaves the page short
        are the two bucket queries appended, after the ranked rows.
        """
        established_tags = Tag.normalize_filter(
            _tags_with(base_tags, f"{_STATUS_NAMESPACE}:established"), kind=self.kind
        )
        hub_tags = Tag.normalize_filter(
            _tags_with(base_tags, _TAPROOT_CLAIM_TAG), kind=self.kind
        )

        pool = self._trust_pool(page_size, trust)
        if q is None or not q.strip():
            established_refs = self.store.list_refs(
                kind=self.kind, tags=established_tags, limit=pool
            )
            hub_refs = self.store.list_refs(kind=self.kind, tags=hub_tags, limit=pool)
            refs = self._apply_trust(
                _merge_dedup(established_refs, hub_refs),
                trust=trust,
                page_size=page_size,
            )
            return self._render_finding_table(refs, query=None)

        wide = self._hybrid_hits(
            q=q,
            tags=Tag.normalize_filter(list(base_tags), kind=self.kind),
            limit=pool * _COHORT_OVERFETCH,
            mode=mode,
        )
        members = self._cohort_members([int(r.id) for r in wide])
        ranked = [r for r in wide if int(r.id) in members]
        if len(ranked) < pool:
            ranked = _merge_dedup(
                ranked,
                _merge_dedup(
                    self._hybrid_hits(
                        q=q, tags=established_tags, limit=pool, mode=mode
                    ),
                    self._hybrid_hits(q=q, tags=hub_tags, limit=pool, mode=mode),
                ),
            )
        refs = self._apply_trust(ranked, trust=trust, page_size=page_size)
        if not refs:
            body = f"no finding matches {q!r} with status='established'"
            if trust:
                body += f" at trust={trust!r}"
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

    #: Publish-state display order for the count view — the trust ladder
    #: most-settled first; ``unminted`` (no publish row) closes it.
    #: Unknown states (future machine additions) append alphabetically.
    _COUNT_STATE_ORDER: ClassVar[tuple[str, ...]] = (
        "published",
        "publishing",
        "anchored",
        "signed",
        "reviewed",
        "candidate",
        "unminted",
    )

    def _finding_count_view(
        self,
        *,
        q: str | None,
        status: str | None,
        trust: str | None,
        tags: list[str],
        group_by: Any,
        since: str | None,
        until: str | None,
    ) -> Response:
        """``search(kind='finding', view='count')`` — filtered total +
        the live claim-graph census (gr518170).

        The head line honours the shared count-view contract
        (:meth:`NumericRefHandler._count_view`): ``status=`` desugars to
        the ``STATUS:`` tag like the hit path, ``q=`` matches
        ``finding_body`` chunks (``q_in_chunks=True`` — the hybrid legs
        rank over chunk text, so the count must filter it), and
        ``group_by=`` takes the same ``state``/``meta.<key>``/
        ``tag:<axis>``/``fulltext`` grammar.

        The census below it is **corpus-level** — the filters above do
        not narrow it (edge/hub totals filtered by a finding query
        would mislabel; the gripe's "1,552 hubs and 2,059 edges" figure
        is whole-corpus). Hubs by publish state and by the
        :data:`FINDING_TRUST_TIERS` predicates come from one
        :func:`~precis.nanopub.overview.hub_rows` pass — the canonical
        posture read, so the tier numbers mean exactly what
        ``trust=`` does on the hit path (they overlap: a signed hub
        usually also answers ``verified``). Edge counts are
        :func:`~precis.nanopub.overview.hub_edge_census`.

        ``trust=`` refuses rather than silently drops — a trust-tier
        read already stands in for the filter, and narrowing edges by
        tier would need a per-hub posture join the census deliberately
        doesn't carry.
        """
        from collections import Counter

        from precis.handlers._numeric_ref import (
            _COUNT_GROUP_LIMIT,
            _count_facet_label,
            _parse_count_facets,
            _parse_window_bound,
        )
        from precis.nanopub.overview import (
            hub_edge_census,
            hub_rows,
            passes_trust,
        )

        if trust is not None:
            raise BadInput(
                "trust= doesn't apply to view='count' — the census "
                "already breaks hubs down by tier",
                next=(
                    "search(kind='finding', view='count') and read the 'trust:' line"
                ),
            )
        facets = _parse_count_facets(group_by)
        since_dt = _parse_window_bound(since, "since")
        until_dt = _parse_window_bound(until, "until")

        effective = list(tags)
        resolved_status = (status or "").strip()
        if resolved_status and resolved_status != "*":
            tag_str = f"{_STATUS_NAMESPACE}:{resolved_status}"
            if tag_str not in effective:
                effective.append(tag_str)
        normalized = Tag.normalize_filter(
            effective or None, kind=self.kind, axis_in_use=self._axis_in_use
        )

        total, groups = self.store.count_refs_facets(
            kind=self.kind,
            tags=normalized,
            q=q,
            q_in_chunks=True,
            facets=facets,
            group_limit=_COUNT_GROUP_LIMIT,
            since=since_dt,
            until=until_dt,
        )
        filters: list[str] = []
        if q and q.strip():
            filters.append(f"q={q!r}")
        if normalized:
            filters.append(f"tags={normalized!r}")
        if since_dt is not None or until_dt is not None:
            lo = since_dt.strftime("%Y-%m-%dT%H:%M:%SZ") if since_dt else "…"
            hi = until_dt.strftime("%Y-%m-%dT%H:%M:%SZ") if until_dt else "…"
            filters.append(f"created [{lo}, {hi})")
        head = f"# {total} {self._sense()} entr{'y' if total == 1 else 'ies'}"
        if filters:
            head += f" ({', '.join(filters)})"
        parts = [head]
        if facets:
            parts.append("")
            parts.append(" × ".join(_count_facet_label(f) for f in facets) + ":")
            for vals, n in groups:
                label = " | ".join(v if v is not None else "(none)" for v in vals)
                parts.append(f"  {label}: {n}")
            if len(groups) == _COUNT_GROUP_LIMIT:
                parts.append(f"  … (top {_COUNT_GROUP_LIMIT} groups by count)")

        rows = hub_rows(self.store)
        states: Counter[str] = Counter(r.state or "unminted" for r in rows)
        order = {s: i for i, s in enumerate(self._COUNT_STATE_ORDER)}
        state_pairs = sorted(
            states.items(), key=lambda kv: (order.get(kv[0], len(order)), kv[0])
        )
        tiers = {
            t: sum(1 for r in rows if passes_trust(r, t))
            for t in FINDING_TRUST_TIERS
            if t != "any"
        }
        census = hub_edge_census(self.store)
        edge_total = sum(n for rels in census.values() for n in rels.values())
        parts.append("")
        parts.append(f"claim hubs: {len(rows)} live")
        if state_pairs:
            parts.append(
                "  publish state: " + " · ".join(f"{s} {n}" for s, n in state_pairs)
            )
            parts.append(
                "  trust: "
                + " · ".join(f"{t} {n}" for t, n in tiers.items())
                + " (tiers overlap — a hub may clear several)"
            )
        parts.append(f"edges on live hubs: {edge_total}")
        for bucket, label in (
            ("evidence", "evidence"),
            ("claim", "claim links"),
            ("lineage", "lineage"),
        ):
            rels = census.get(bucket)
            if not rels:
                continue
            parts.append(
                f"  {label}: "
                + " · ".join(
                    f"{rel} {n}"
                    for rel, n in sorted(rels.items(), key=lambda kv: (-kv[1], kv[0]))
                )
            )
        return Response(body="\n".join(parts), result_count=total)

    def _render_finding_table(self, refs: list[Ref], *, query: str | None) -> Response:
        """Render the finding-search TOON table.

        Shape: ``id | title | setup | primary``, widened to ``id | title |
        state | support | flags | setup | primary`` as soon as **any** hit
        is a claim hub. ``setup`` is ``meta.scope`` flattened to
        ``key=value`` pairs; ``primary`` is ``meta.primary_cite_key`` when
        the chase has terminated (empty for in-flight rows).

        **Why the posture columns.** Before them the table was
        epistemically blind: a hub minted an hour ago and born withheld
        rendered identically to one signed, anchored and published, so
        nothing in the result told a writer which claims the corpus
        actually stands behind — and the settled layer is only worth
        searching first if you can see that it is settled. ``state`` is
        the publish rung (or ``unminted``), ``support`` reads
        ``2✓ 1✗ 1?`` — affirmative verdicts, negative verdicts (an edge
        *can* be verified and refuting), and withheld ones the publish
        preflight will refuse — and ``flags`` carries the rest —
        ``disputed`` (a live ``contradicts`` edge), ``drifted`` (the
        claim was reworded out from under its frozen string), and
        ``refuted`` (every judged edge came back negative — ``supported_
        count == 0`` while ``verified_count > 0``; a hub with only
        *unjudged* evidence is not refuted, it is unverified). The
        negatives ride in the same row as the hit on purpose: a claim
        layer that shows only its promotions is how a contradicted claim
        gets laundered into a draft.

        The three columns are omitted entirely when no hit carries a
        posture — a plain ``STATUS:established`` chase finding has no
        publish row and never will, so an established-only result stays
        exactly as narrow as it was.
        """
        from precis.format import render_agent_table

        if not refs:
            return Response(body="no finding entries match")

        postures = self._postures([int(r.id) for r in refs])
        rows: list[dict[str, str]] = []
        for r in refs:
            meta = r.meta or {}
            scope = meta.get("scope") or {}
            setup_str = ", ".join(f"{k}={v}" for k, v in sorted(scope.items()) if v)
            primary = meta.get("primary_cite_key") or ""
            row = {
                "id": str(r.id),
                "title": r.title,
                "setup": setup_str,
                "primary": primary,
            }
            row.update(_posture_cells(postures.get(int(r.id))))
            rows.append(row)

        schema = ["id", "title", "setup", "primary"]
        if postures:
            schema = ["id", "title", "state", "support", "flags", "setup", "primary"]
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

    def edit(
        self,
        *,
        id: int | str | list[str | int] | None = None,
        mode: str | None = None,
        find: str | None = None,
        text: str | None = None,
        before: str | None = None,
        after: str | None = None,
        where: str | None = None,
        match: str | None = None,
        nth: int | None = None,
        pick_candidate: str | int | None = None,
        title: str | None = None,
        unacquirable_note: str | None = None,
        unacquirable_mode: str | None = None,
        testable_by: str | None = None,
        motivation: str | None = None,
        reason: str | None = None,
        meta: dict[str, Any] | None = None,
        dry_run: bool | str | None = None,
        refine: str | None = None,
        **_kw: Any,
    ) -> Response:
        """Resolve a ``STATUS:multi_candidate`` finding by picking one cite,
        retitle a ``TAPROOT:claim`` hub, record an author's
        unacquirable-source override, or sharpen a live hypothesis's
        falsification terms (``testable_by=``/``motivation=``), or rescope a
        hub (``meta={'scope': {...}}``). Mutually exclusive kwargs — pass
        exactly one (``testable_by=``/``motivation=`` may be combined with
        each other, ``meta={'scope': …}`` with ``title=``, not with the
        rest); ``dry_run`` is honoured only by the scope door and rejected
        by every other op (no faithful preview). See
        :func:`precis.handlers._finding_edit.edit` for the full contract
        (pick_candidate promotes a chase candidate + flips status back to
        tracing — pick_candidate='self' instead closes out a
        ``STATUS:dead_chain`` finding whose frontier paper is held, and
        ``reason=`` records why; title retitles a TAPROOT:claim hub via
        ``taproot/hub.py::refine_claim_sentence``; meta={'scope': …} replaces
        the hub's scope through the same door (scope is part of the
        content-derived pub_id, so it is an identity edit); unacquirable_note writes
        the trust-surfaces override; testable_by=/motivation= route through
        ``_finding_hypothesis.update_hypothesis``) — the state machine lives
        there since it only ever touches ``self.store``/``self.kind``, not
        any other handler state.

        A body edit (``mode='find-replace'`` with ``find=`` + ``text=``, or
        ``mode='replace'`` with ``text=``) rewrites a plain finding's
        ``finding_body`` chunk — the correction door for a wrong or
        overreaching clause (gr509620); delete+insert, old text kept in a
        ``body_replaced`` ref_event, and the ``meta.chain`` chunk
        citations re-validated in the same transaction. Refused on a
        ``TAPROOT:claim`` hub (``title=`` is the in-sync rewrite there).

        ``refine='now'`` (hub only, exclusive with every other kwarg except
        ``dry_run``) runs the hub_refine pass over that one hub synchronously
        (:func:`precis.workers.hub_refine.refine_hub_now`) and reports the
        pass counters; the attempt lease and regrounding gates apply.

        ``id=[...]`` (``batch_ids``) applies the same edit to every id in
        one transaction — :meth:`_edit_batch` (gr514482).
        """
        if isinstance(id, list):
            return self._edit_batch(
                id,
                mode=mode,
                find=find,
                text=text,
                before=before,
                after=after,
                where=where,
                match=match,
                nth=nth,
                pick_candidate=pick_candidate,
                title=title,
                unacquirable_note=unacquirable_note,
                unacquirable_mode=unacquirable_mode,
                testable_by=testable_by,
                motivation=motivation,
                reason=reason,
                meta=meta,
                dry_run=dry_run,
                refine=refine,
                **_kw,
            )
        if refine is not None:
            if any(
                v is not None
                for v in (
                    find,
                    text,
                    before,
                    after,
                    where,
                    nth,
                    pick_candidate,
                    title,
                    unacquirable_note,
                    unacquirable_mode,
                    testable_by,
                    motivation,
                    reason,
                    meta,
                )
            ) or (match is not None and match != "unique"):
                raise BadInput(
                    "refine='now' is exclusive with the other edit kwargs",
                    next="edit(kind='finding', id='fi<N>', refine='now')  "
                    "# alone, no other kwargs",
                )
            return self._refine_now(id, refine, dry_run)
        resp = _finding_edit.edit(
            self.store,
            kind=self.kind,
            id=id,
            mode=mode,
            find=find,
            text=text,
            before=before,
            after=after,
            where=where,
            match=match,
            nth=nth,
            pick_candidate=pick_candidate,
            title=title,
            unacquirable_note=unacquirable_note,
            unacquirable_mode=unacquirable_mode,
            testable_by=testable_by,
            motivation=motivation,
            reason=reason,
            meta=meta,
            dry_run=dry_run,
        )
        # A prose door (body rewrite ``text=`` or hub retitle ``title=``)
        # re-resolves mention autolinks against the new text — removed
        # mentions drop their auto edge, new C-numbers link their part
        # ref (gr520243). ``_sync_mention_links`` is best-effort; the
        # edit's own response stands either way.
        from precis.utils.edit_resolve import normalize_dry_run

        if (
            id is not None
            and (text is not None or title is not None)
            and normalize_dry_run(dry_run) is None
        ):
            ref_id = _finding_edit._resolve_finding_ref_id(
                self.store, kind=self.kind, raw_id=id
            )
            ref = self._resolve_live_ref(ref_id)
            fbody = next(
                (
                    c.text
                    for c in self.store.chunks.list_chunks_for_ref(ref_id)
                    if c.chunk_kind == "finding_body"
                ),
                "",
            )
            with self.store.tx() as conn:
                facts_deferred = self._sync_mention_links(
                    ref_id,
                    (ref.title or "") + "\n" + fbody,
                    conn=conn,
                    replace=True,
                )
            resp = self._facts_note(resp, facts_deferred)
        return resp

    def _refine_now(
        self, id: int | str | None, refine: str, dry_run: bool | str | None
    ) -> Response:
        from precis.taproot.authoring import resolve_hub_ref_id
        from precis.workers.hub_refine import refine_hub_now

        if str(refine).strip().lower() != "now":
            raise BadInput(
                "refine= accepts only 'now'",
                next="edit(kind='finding', id='fi<N>', refine='now')",
            )
        if id is None:
            raise BadInput(
                "refine='now' needs id=<claim hub>",
                next="edit(kind='finding', id='fi<N>', refine='now')",
            )
        hub_id = resolve_hub_ref_id(self.store, id)
        dry = str(dry_run).strip().lower() in ("1", "true", "yes")
        result = refine_hub_now(
            self.store,
            hub_id,
            embedder=getattr(self.hub, "embedder", None),
            dry_run=dry,
        )
        body = " ".join(f"{k}={v}" for k, v in result.items())
        note = ""
        if result.get("failed"):
            note = " — the refine raised; see the worker log (attempt lease stands)"
        elif not dry and not result.get("claimed"):
            note = (
                " — not claimed: not a claim hub, composite, or an attempt "
                "lease is standing (retry after the cooldown)"
            )
        return Response(body=f"refine fi{hub_id}: {body}{note}")

    # ──────────────────────────────────────────────────────────────────
    # cite — explicitly not supported
    # ──────────────────────────────────────────────────────────────────

    def cite(self, *, id: str | int | None = None, **_kw: Any) -> Response:
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

    def _render_one(self, ref: Ref, tags: Any) -> str:
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

        # Trust-surfaces override (the unacquirable-override door): an
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

        # Acquisition-mode findings: the
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
                pos = link.dst_ord
                # ref-level → record universal handle; block-level
                # keeps the legacy ``slug~pos`` (chunk_id unavailable here).
                if pos is None:
                    handle = (
                        handle_registry.try_format(target.kind, target.id) or legacy
                    )
                else:
                    handle = f"{legacy}~{pos}"
                lines.append(f"  {handle}")

        caveats = self._source_caveats(ref, primary_cite)
        if caveats:
            lines.append("")
            lines.append("source caveats:")
            lines.extend(f"  {handle} — {text}" for handle, text in caveats)

        status = _extract_status_tag(tags)
        lines.append("")
        status_line = f"status: STATUS:{status or _STATUS_TRACING}"
        # dead_chain is eight causally different terminal states behind one
        # lossy tag — the reason chase._set_status recorded (meta.dead_reason)
        # must be legible here, or "acquisition failed" and "well-cited chunk
        # forced an unresolvable hop" are indistinguishable (gr271239).
        dead_reason = meta.get("dead_reason")
        if dead_reason and status == "dead_chain":
            status_line += f" (reason={dead_reason})"
        lines.append(status_line)

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

    def _source_caveats(self, ref: Ref, primary_cite: Any) -> list[tuple[str, str]]:
        """``(paper handle, caveat)`` for each source paper of ``ref`` whose
        text extraction is glyph-suspected (gr228652).

        Source papers = the kind='paper' targets of the finding's outbound
        links (covers taproot supporters) plus the paper named by
        ``meta.primary_cite_key``. A ``misattributes`` link is a disowned
        source, not one, so it is skipped. One batched ref fetch, no
        per-link query.
        """
        ids = [
            lk.dst_ref_id
            for lk in self.store.links_for(ref.id, direction="out")
            if lk.relation != "misattributes"
        ]
        papers = list(self.store.fetch_refs_by_ids(ids, include_deleted=False).values())
        if primary_cite:
            prim = self.store.get_ref(kind="paper", id=str(primary_cite))
            if prim is not None:
                papers.append(prim)
        out: list[tuple[str, str]] = []
        seen: set[int] = set()
        for p in sorted(papers, key=lambda r: r.id):
            if p.kind != "paper" or p.id in seen:
                continue
            seen.add(p.id)
            caveat = glyph_caveat(p.meta)
            if caveat:
                handle = handle_registry.try_format("paper", p.id) or (
                    p.slug or f"ref:{p.id}"
                )
                out.append((handle, caveat))
        return out

    def _fetch_ref_any_kind(self, ref_id: int) -> Ref:
        """Look up a ref by id without knowing its kind.

        Thin wrapper around the shared
        :func:`precis.handlers._finding_common.fetch_ref_any_kind` —
        kept as a method since ``put()`` and ``_render_one`` (both still
        on this class) call it as ``self._fetch_ref_any_kind(...)``.
        """
        return fetch_ref_any_kind(self.store, ref_id)

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


def is_refuted(row: HubOverviewRow) -> bool:
    """``refuted``: every judged evidence edge came back negative —
    ``supported_count == 0`` while ``verified_count > 0``. A hub whose
    edges are merely *unjudged* (``verified_count == 0`` too) is not
    refuted, it is unverified — flagging it here would be the exact
    inversion gr263023 warns against. Shared by :func:`_posture_cells`
    (the search-table ``flags`` column) and ``handlers/draft.py``'s
    hygiene view (the cite-time signal), so the two never drift apart."""
    return row.supported_count == 0 and row.verified_count > 0


def _hub_eye_header(store: Store, ref: Ref) -> str:
    """The leading posture line an eye on a claim hub carries.

    This is why the eye is legible — a hub's neighborhood without its
    trust posture reads as settled fact regardless of whether anything
    supports it. Reuses :func:`_posture_cells` (the same cells the
    ``search`` table's ``state``/``support``/``flags`` columns render), so
    the eye and the search table can never disagree.

    Returns ``""`` for a non-hub finding — a plain chase-tree finding has
    no publish posture to carry."""
    if not is_claim_hub(store, int(ref.id)):
        return ""
    from precis.nanopub.overview import hub_rows

    rows = hub_rows(store, ref_ids=[int(ref.id)])
    row = rows[0] if rows else None
    cells = _posture_cells(row)
    parts = [f"{k}: {v}" for k, v in cells.items() if v]
    # `` · withheld N · removed M`` — judge actions that kept an edge out or
    # took one off; absent (byte-identical header) for an untouched hub.
    judged = _finding_judgments.posture_suffix(ref.meta)
    if not parts:
        return "◆ claim hub — no publish posture yet" + judged + "\n\n"
    return "◆ claim hub — " + " · ".join(parts) + judged + "\n\n"


def _posture_cells(row: HubOverviewRow | None) -> dict[str, str]:
    """The ``state``/``support``/``flags`` cells for one hit.

    ``support`` reads ``2✓ 1✗ 1?`` — affirmative verdicts, *negative*
    verdicts, withheld. The ✗ is not decoration: an edge whose verdict is
    ``support: "no"`` is verified and supports nothing, so folding it in
    with the ✓ (as this did until it was caught on the shipped diff) would
    render a refuted hub as a well-supported one.
    """
    if row is None:
        return {"state": "", "support": "", "flags": ""}
    support = ""
    negative = row.verified_count - row.supported_count
    if row.verified_count or row.withheld_count:
        support = f"{row.supported_count}✓"
        if negative > 0:
            support += f" {negative}✗"
        if row.withheld_count:
            support += f" {row.withheld_count}?"
    # A composite's evidence hangs off its atoms: roll them up so it never
    # reads as "no verdicts" while every atom is supported.
    if row.conjunct_count:
        atoms = f"atoms {row.conjuncts_supported}/{row.conjunct_count}✓"
        support = f"{support} {atoms}" if support else atoms
    refuted = is_refuted(row)
    flags = [
        f
        for f, on in (
            ("disputed", row.disputed),
            ("drifted", row.drifted),
            ("refuted", refuted),
        )
        if on
    ]
    if row.conjuncts_contradicted:
        flags.append(f"atoms-contradicted:{row.conjuncts_contradicted}")
    # Non-blocking open-question count (D1, migration 0151) — a distinct
    # marker from ``disputed`` above (which is the adjudicated, blocking
    # `contradicts` shape): a live `disputes` edge is never a demerit, so
    # it must never read as the same flag.
    if row.open_disputes_count:
        flags.append(f"open-questions:{row.open_disputes_count}")
    return {
        "state": row.state or "unminted",
        "support": support,
        "flags": ",".join(flags),
    }


def _search_hit_posture(row: HubOverviewRow) -> str:
    """The cross-kind ``SearchHit.posture`` token for one claim hub.

    Terser than :func:`_posture_cells` (three TOON columns) — one
    pre-rendered prefix for the merged cross-kind summary cell
    (the claim-layer-in-cross-kind-search design (shipped 2026-09-19) option 1).
    Bucket order is significant, both for what a drafting agent needs to
    see first and for the ranking lever that pattern-matches this exact
    vocabulary (``search_merge._merge_rrf``, ``_POSTURE_REFUTED_PREFIX``/
    ``_POSTURE_VERIFIED_MARKER``): refuted first (rare, high-value
    negative signal — every judged edge came back negative), then
    disputed (a live ``contradicts`` edge — contested, never demoted, the
    RRF lever leaves it at its unpenalised rank), then verified-and-
    unopposed (the dense positive signal — reuses
    :func:`precis.nanopub.overview.passes_trust`'s
    own ``trust='verified'`` definition: ``supported_count > 0`` — or, for
    a composite, every conjunct atom supported — and not disputed), else
    unverified (unminted-with-nothing or withheld-only —
    the bulk of the corpus per the backlog item's prod snapshot).
    """
    if is_refuted(row):
        return "◆ refuted"
    if row.disputed:
        return "◆ disputed"
    if row.supported_count > 0:
        return f"◆ {row.supported_count}✓ unopposed"
    if row.atoms_all_supported:
        return f"◆ atoms {row.conjuncts_supported}/{row.conjunct_count}✓ unopposed"
    return "◆ unverified"


__all__ = ["FindingHandler"]
