"""Tiny shared helper used by ``finding.py`` and its split-out helper
modules (``_finding_acquire``, ``_finding_edit``, ``_finding_evidence``).

Kept separate (rather than importing from ``finding.py`` itself) so those
helper modules don't have to import back into the module that imports
them — ``finding.py``'s ``FindingHandler`` pulls all of these in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from precis.errors import BadInput
from precis.handlers._gripe_title import cut_at_word_boundary
from precis.store.types import ActorSlug, Ref, Relation, Tag
from precis.taproot import hub as hublib
from precis.taproot.canon import TAPROOT_NAMESPACE
from precis.utils import handle_registry
from precis.utils.link_tag_ops import (
    apply_tag_ops,
    check_functional_slot,
    resolve_add_link,
    validate_relation,
)
from precis.utils.link_target import LinkTarget

if TYPE_CHECKING:
    from precis.store import Store

#: ``refs.title`` cap for a finding. ``put`` silently cuts an over-long
#: ``title=`` at a word boundary — Reto's gripe-title ruling ("silently
#: cut, put it inside", gr538410): the full original text goes to the
#: top of the finding body, so nothing is lost and ``edit(title=)``
#: (hub-only anyway) is never the fix door.
FINDING_TITLE_MAX = 80


def cap_finding_title(title: str, body: str) -> tuple[str, str]:
    """Cap a finding title at :data:`FINDING_TITLE_MAX`; returns
    ``(title, body)``, both stripped.

    An over-long title is cut at a word boundary and the full original
    text is kept at the top of ``body`` when the body doesn't already
    lead with it — the gripe-title ruling applied to findings
    (gr538410). A title at or under the cap passes through.
    """
    title = title.strip()
    body = body.strip()
    if len(title) <= FINDING_TITLE_MAX:
        return title, body
    if not body.startswith(title):
        body = f"{title}\n\n{body}" if body else title
    return cut_at_word_boundary(title, FINDING_TITLE_MAX), body


def fetch_ref_any_kind(store: Store, ref_id: int) -> Ref:
    """Look up a ref by id without knowing its kind.

    The store's ``get_ref`` API requires ``kind``; ``parse_link_target``
    returns the resolved kind on the ``LinkTarget`` so callers can
    round-trip. Re-fetching here reads the slug (cite_key) for the
    deterministic pub_id input, and is reused everywhere a finding-family
    caller needs a handle for an arbitrary link endpoint (put's cited_in
    resolution, the candidate-pick / evidence-render paths, the begat
    detail render).
    """
    from precis.store._mappers import _REFS_COLS, _row_to_ref

    with store.pool.connection() as conn:
        row = conn.execute(
            f"SELECT {_REFS_COLS} FROM refs WHERE ref_id = %s AND retired_at IS NULL",
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


@dataclass(frozen=True, slots=True)
class PutExtras:
    """A claim-hub put's create-time ``tags=``/``link=``/``rel=``, checked
    by :func:`check_put_extras` before any write."""

    tags: tuple[str, ...]
    target: LinkTarget | None
    relation: Relation


#: Closed axes ``mint_hub`` stamps on every claim hub. A closed-prefix add
#: replaces the value already there, so a create-time tag on one of these
#: would displace the stamp — and on a converge, an existing hub's status.
_MINT_STAMPED_AXES = frozenset({"STATUS", TAPROOT_NAMESPACE})


def check_put_extras(
    store: Store,
    *,
    tags: list[str] | None,
    link: str | None,
    rel: str | None,
    hub: Any = None,
) -> PutExtras | None:
    """Check a claim-hub put's ``tags=``/``link=``/``rel=`` before any write;
    ``None`` when the call carries none (gr676914).

    The hub and hypothesis doors used to accept the three and drop them, so
    a caller redid them with a ``tag`` and a ``link`` call after every mint.
    Everything that can be refused without the hub is refused here, so a
    bad extra never leaves a hub the failed put did not name (gr263195):

    * a malformed tag, and a closed ``STATUS:``/``TAPROOT:`` tag (the mint
      stamps both axes itself);
    * an unregistered ``rel=``, or ``rel=`` without ``link=``;
    * an evidence role as ``rel=`` — evidence runs paper -> hub and enters
      through ``supporters=``; a raw hub -> paper row would bypass
      :func:`~precis.taproot.hub.attach_evidence`;
    * a claim-link relation whose target is not a finding;
    * a ``link=`` target that is malformed or resolves to no live ref;
    * an edge the ``link`` verb refuses — the taxon hierarchy rules and the
      relation's constraint row, through the shared
      :func:`~precis.utils.link_tag_ops.resolve_add_link` (gr753894). The
      hub is not known yet, so the source is checked as a new finding; a
      ``functional`` slot held by another finding is left to
      :func:`apply_put_extras`, because a converging put may land on the
      very hub that holds it.

    A bare ``link=`` defaults to ``cites``, as on the chase and acquisition
    doors.
    """
    for raw in tags or []:
        tag = Tag.parse_strict(raw, kind="finding")
        if tag.namespace == "closed" and tag.prefix in _MINT_STAMPED_AXES:
            raise BadInput(
                f"{raw} cannot ride a claim-hub put — the mint stamps "
                "STATUS:canonical and TAPROOT:claim itself, and a create-time "
                "tag on either axis would replace the stamp",
                next="drop the tag — the hub is born STATUS:canonical, TAPROOT:claim",
            )
    relation = validate_relation(rel or "cites", store=store)
    target: LinkTarget | None = None
    if link is None:
        if rel is not None:
            raise BadInput(
                "rel= requires link= on create",
                next=f"put(kind='finding', ..., link='<handle>', rel={rel!r})",
            )
    else:
        if relation in hublib.HUB_ROLES:
            raise BadInput(
                f"rel={relation!r} is an evidence role — evidence runs paper -> "
                "hub and enters a claim hub through supporters=, not link=",
                next="supporters=[{'paper': 'pc<id>', 'role': 'corroborates', "
                "'source_handle': 'pc<id>'}] carries evidence; a conflict "
                "with another claim hub is link='fi<id>', rel='disputes'",
            )
        target = resolve_add_link(
            store,
            None,
            link,
            relation,
            src_kind="finding",
            src_may_exist=True,
            hub=hub,
        )
        if relation in hublib.CLAIM_LINK_RELATIONS and (
            target.kind != "finding" or target.pos is not None
        ):
            raise BadInput(
                f"rel={relation!r} joins two claim hubs — link={link!r} is not one",
                next=f"link='fi<id>' (a claim hub), rel={relation!r}",
            )
    if not tags and target is None:
        return None
    return PutExtras(tags=tuple(tags or ()), target=target, relation=relation)


def apply_put_extras(
    store: Store,
    extras: PutExtras,
    hub_ref_id: int,
    *,
    conn: Any,
    set_by: ActorSlug = "agent",
) -> str:
    """Write checked ``extras`` onto ``hub_ref_id`` on the caller's open
    transaction; returns the response line that names the hub (gr676914).

    ``hub_ref_id`` is whichever hub the put resolved to — the minted one,
    or the hub an attach/converge outcome matched — so the line tells the
    caller where the tags and link landed. A refusal here (a self-link, a
    claim-link to a non-hub, the store's own edge admission) raises inside
    the caller's transaction and takes the mint or attach with it.

    A claim-link relation goes through
    :func:`~precis.taproot.hub.link_claims`, the door the ``link`` verb
    uses for a hub source; every other relation is a plain ``add_link``,
    as on the chase door.
    """
    handle = handle_registry.format_handle("finding", hub_ref_id)
    done: list[str] = []
    if extras.tags:
        apply_tag_ops(
            store, "finding", hub_ref_id, tags=list(extras.tags), untags=None, conn=conn
        )
        done.append("tags " + ", ".join(extras.tags))
    target = extras.target
    if target is not None:
        if target.ref_id == hub_ref_id and target.pos is None:
            raise BadInput(
                f"link={target.raw!r} is {handle}, the hub this put resolves "
                "to — a hub cannot link to itself",
                next="drop link= and put again — nothing was written",
            )
        # The functional slot, now that the hub is known: free, or held by
        # this very hub (a converging put re-adding its own edge).
        check_functional_slot(store, extras.relation, hub_ref_id, target, conn=conn)
        if extras.relation in hublib.CLAIM_LINK_RELATIONS:
            hublib.link_claims(
                store,
                from_hub_ref_id=hub_ref_id,
                to_hub_ref_id=target.ref_id,
                relation=extras.relation,
                set_by=set_by,
                conn=conn,
            )
        else:
            store.add_link(
                src_ref_id=hub_ref_id,
                dst_ref_id=target.ref_id,
                dst_pos=target.pos,
                relation=extras.relation,
                set_by=set_by,
                conn=conn,
            )
        done.append(f"link --{extras.relation}--> {target.raw}")
    return f"applied to {handle}: " + "; ".join(done)


def multi_article_advisory(store: Store, paper_ref_id: int, handle: str) -> str:
    """One-line advisory when the cited paper is tagged ``multi-article``
    (gr463414), else ``""``. The write still succeeds; the agent confirms
    the cited passage belongs to this paper before relying on the edge."""
    if not store.is_multi_article(paper_ref_id):
        return ""
    return (
        f"\nadvisory: {handle}'s stored body bundles several articles "
        "(tag multi-article) - confirm the cited passage belongs to this "
        "paper (its page carries this paper's own title or DOI) before "
        "relying on the edge"
    )
