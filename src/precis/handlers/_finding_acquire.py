"""Acquisition-mode (claim-first) mint — ``put(kind='finding', wants=...)``.

Split out of ``finding.py`` (docs/backlog/codereview-handler-size-cleanups.md):
this was the single largest cohesive block in that handler (~290 lines) and
touches nothing from ``NumericRefHandler``'s CRUD contract — only the store
and a small callback for the pub_id-collision response, both passed in
explicitly. ``FindingHandler.put`` calls :func:`put_acquiring` directly for
the ``wants=`` branch; there is no method left on the handler for it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from psycopg.errors import UniqueViolation

from precis.errors import BadInput
from precis.handlers._link_tag_ops import apply_tag_ops
from precis.handlers._link_target import LinkTarget, parse_link_target
from precis.identity import make_finding_paper_id, make_pub_id
from precis.response import Response
from precis.store.types import BlockInsert, Tag
from precis.utils import handle_registry

if TYPE_CHECKING:
    from precis.store import Store

_STATUS_NAMESPACE = "STATUS"
_STATUS_ACQUIRING = "acquiring"
_DERIVED_FROM = "derived-from"
_AWAITS_EVIDENCE = "awaits-evidence"


@dataclass(frozen=True, slots=True)
class WantDescriptor:
    """One parsed ``wants=`` entry (the acquisition-mode paper descriptor).

    Exactly one of ``doi`` / ``arxiv`` / (``title`` and ``url``) is
    guaranteed non-None by :func:`parse_want` — ``title`` and ``url`` may
    additionally ride along on a doi/arxiv descriptor as enrichment (a
    nicer stub title, an informational landing-page url).
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


def parse_want(index: int, want: Any) -> WantDescriptor:
    """Parse one ``wants=`` entry into a :class:`WantDescriptor`.

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
    return WantDescriptor(doi=doi, arxiv=arxiv, title=w_title, url=url, year=year)


def put_acquiring(
    store: Store,
    *,
    kind: str,
    title: str | None,
    body_text: str | None,
    scope: dict[str, Any] | None,
    wants: list[dict[str, Any]],
    provenance: str | None,
    parent_id: int | None,
    tags: list[str] | None,
    link: str | None,
    rel: str | None,
    collision_response: Callable[[str], Response],
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
    ``fetch_oa``. ``collision_response`` builds the "already exists"
    ack for a pub_id collision — the caller's
    ``FindingHandler._collision_response``.
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
        missing.append("provenance=<ref/chunk handle for where this claim came from>")
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

    parsed_wants = [parse_want(i, w) for i, w in enumerate(wants)]

    # Resolve provenance up front — a bad handle fails before any
    # write, mirroring the ordinary mode's cited_in resolution.
    provenance_target = parse_link_target(str(provenance).strip(), store=store)

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
        extra_target = parse_link_target(link, store=store)

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
        with store.tx() as conn:
            ref = store.insert_ref(
                kind=kind,
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
            store.blocks.insert_blocks(
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
            store.add_tag(
                ref.id,
                Tag.closed(_STATUS_NAMESPACE, _STATUS_ACQUIRING),
                set_by="agent",
                replace_prefix=True,
                conn=conn,
            )
            apply_tag_ops(store, kind, ref.id, tags=tags, untags=None, conn=conn)
            store.add_link(
                src_ref_id=ref.id,
                dst_ref_id=provenance_target.ref_id,
                dst_pos=provenance_target.pos,
                relation=_DERIVED_FROM,
                conn=conn,
            )
            if extra_target is not None:
                store.add_link(
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
                stub_ref_id, created = store.upsert_stub_paper(
                    identifiers=identifiers,
                    title=w.title,
                    year=w.year,
                    set_by="dream",
                    conn=conn,
                )
                if created:
                    store.add_tag(
                        stub_ref_id,
                        Tag.closed("DREAM", "acquire"),
                        set_by="agent",
                        conn=conn,
                    )
                if w.url:
                    # Informational only in this build — no fetch leg
                    # reads a bare URL yet (explicitly out of the
                    # acquisition-mode scope); a human sees it via
                    # get(kind='paper', id=<stub>).
                    store.update_ref(
                        stub_ref_id,
                        meta_patch={"acquire_url": w.url},
                        conn=conn,
                    )
                store.add_link(
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
        return collision_response(pub_id)

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
