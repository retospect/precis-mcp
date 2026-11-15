"""Tiny shared helper used by ``finding.py`` and its split-out helper
modules (``_finding_acquire``, ``_finding_edit``, ``_finding_evidence``).

Kept separate (rather than importing from ``finding.py`` itself) so those
helper modules don't have to import back into the module that imports
them — ``finding.py``'s ``FindingHandler`` pulls all of these in.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from precis.errors import BadInput
from precis.handlers._gripe_title import cut_at_word_boundary
from precis.response import Response
from precis.store.types import Ref, Relation, Tag
from precis.taproot.canon import STATUS_CANONICAL
from precis.utils import handle_registry
from precis.utils.link_tag_ops import (
    apply_tag_ops,
    format_link_tag_ack,
    validate_relation,
)
from precis.utils.link_target import LinkTarget, parse_link_target

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


def check_put_extras(
    store: Store,
    *,
    kind: str,
    tags: list[str] | None,
    link: str | None,
    rel: str | None,
) -> tuple[LinkTarget | None, Relation]:
    """Resolve a ``put``'s create-time ``tags=``/``link=``/``rel=`` extras
    BEFORE the mint doors write anything (gr676914).

    The hub and hypothesis mint doors took the same ``tags=``/``link=``/
    ``rel=`` kwargs the ``put`` schema declares and silently dropped them —
    callers then spent two more calls redoing by hand what the put should
    have done. A malformed tag or an unresolvable ``link=`` target must
    raise here: after a write it would strand a hub the refused put never
    names (the gr263195 no-stranded-write rule).

    A create-time closed ``STATUS:`` tag on any value but the mint's own
    ``STATUS:canonical`` refuses here too — the mint doors stamp
    ``STATUS:canonical`` on the hub themselves, and a closed-prefix add
    *replaces*, so the hand tag would displace the canonical stamp and drop
    the hub from the search cohort. This is the A3 refusal
    (``_refuse_hand_set_established`` in ``finding.py``, which guards the
    chase value ``established`` at the put/tag doors) generalised to every
    conflicting value, in both mint modes, before any write (gr676914
    review).

    Returns ``(target, relation)`` for :func:`apply_put_extras`. The
    relation defaults to ``'cites'`` — the same spelling the chase and
    acquisition doors give a bare create-time ``link=``.
    """
    for t in tags or []:
        parsed = Tag.parse_strict(t, kind=kind)
        if (
            parsed.namespace == "closed"
            and parsed.prefix == "STATUS"
            and parsed.value != STATUS_CANONICAL
        ):
            raise BadInput(
                f"STATUS:{parsed.value} can't ride a create-time put on a "
                "claim hub — the mint stamps STATUS:canonical itself, and "
                "the conflicting tag would displace it and drop the hub "
                "from the search cohort",
                next="drop the STATUS tag — the hub is born canonical; the "
                "chase-lifecycle values are the chase worker's to write",
            )
    if link is None:
        if rel is not None:
            raise BadInput(
                "rel= requires link= on create",
                next=f"put(kind={kind!r}, ..., link='<handle>', rel={rel!r})",
            )
        return None, "cites"
    relation = validate_relation(rel or "cites", store=store)
    if relation == "rejects":
        raise BadInput(
            "rel='rejects' needs a meta.reason that a create-time link= cannot carry",
            next="mint first, then link(kind='finding', id='<fi>', "
            "target='<handle>', rel='rejects', meta={'reason': '...'})",
        )
    return parse_link_target(link, store=store), relation


def apply_put_extras(
    store: Store,
    kind: str,
    ref_id: int,
    resp: Response,
    *,
    tags: list[str] | None,
    target: LinkTarget | None,
    relation: Relation,
) -> Response:
    """Write a ``put``'s create-time extras onto the ref the outcome named
    and append the ack line (gr676914).

    The extras land on whichever ref the response points at — the freshly
    minted hub on a mint outcome, the matched neighbour on an attach/
    converge outcome — so an agent that asked for ``tags=``/``link=`` sees
    them carried out, with the ack naming the ref that received them.
    """
    if not tags and target is None:
        return resp
    n_tags = n_links = 0
    with store.tx() as conn:
        if tags:
            n_tags, _ = apply_tag_ops(
                store, kind, ref_id, tags=tags, untags=None, conn=conn
            )
        if target is not None:
            store.add_link(
                src_ref_id=ref_id,
                dst_ref_id=target.ref_id,
                dst_pos=target.pos,
                relation=relation,
                conn=conn,
            )
            n_links = 1
    handle = handle_registry.format_handle(kind, ref_id)
    ack = format_link_tag_ack(
        kind=kind,
        ref_label=handle,
        n_links_added=n_links,
        n_links_removed=0,
        n_tags_added=n_tags,
        n_tags_removed=0,
    )
    return replace(resp, body=f"{resp.body}\n{ack}")


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
