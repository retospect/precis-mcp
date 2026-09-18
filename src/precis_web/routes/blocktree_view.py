"""The ``se`` web reader — round 1 (docs/backlog thread pending
its own transcription pass; the spec of record is prod gripe gr335242,
items 1-3): project a design's posed envelope union to SVG, isolate a
named subtree, and step through a discrete abstraction ladder. Built as
a kind-keyed adapter table over the shared projector
(:mod:`precis_web.blocktree_svg`) because it served ``se`` *and* ``nm``
— both :mod:`precis.blocktree` subclasses carrying the same L1
envelope/pose triple (module docstring there). The nm→se merge
(docs/backlog/nm-se-merge.md) retired that kind, so ``se`` is the only
adapter today; the table stays because the shape is the seam a second
block-tree kind would plug into, and se's own atomic mode arrived
through it.

* ``GET  /se`` — the design list (mirrors ``routes/cad.py``'s ``/cad``).
* ``GET  /se/{slug}`` — the reader page, landing on
  the three-cad-viewer 3D view by default (gr337745 — the 2D SVG
  projection is depthless/overlap-heavy for a real multi-block design
  and no longer earns the default slot). Query params: ``level`` (the
  named abstraction ladder, default 'refined'), ``isolate`` (a block
  name — render only its subtree, recentred), ``overrides`` (round 2a —
  ``"name:level, name2:level2"``, per-subtree level override; see
  :mod:`precis_web.blocktree_svg`'s ``plan_visibility`` docstring).
* ``GET  /se/{slug}/scene3d.json`` —
  the data the 3D page fetches: the viewer's own ``Shapes`` tree plus
  the connectivity/explode/mermaid side data
  (:class:`~precis_web.blocktree_3d.Scene3D`).
* ``GET  /se/{slug}/2d`` — the 2D SVG reader
  page, still reachable via a link off the 3D page: axis/level/colour/
  isolate selectors (a plain GET form — no client JS needed) over an
  ``<img>`` of the SVG endpoint below. Each page links the other.
* ``GET  /se/{slug}/view.svg`` — the SVG
  render itself. Same params as ``/2d`` plus ``axis`` (x|y|z, default z
  — top view) and ``colour`` (the fill channel, default 'part') — both
  SVG-projection-only, so absent from the 3D/``/2d`` page URLs above.

Round 2a added the three-cad-viewer 3D route (gr335242 comment 5 / spec
§5.8) off the same shared plan (:mod:`precis_web.blocktree_3d`, kept
SEPARATE from the SVG projection math but sharing
``plan_visibility``/``children_map`` so the level ladder, isolation, and
override behave identically in both readers) — gr337745 later promoted
it to the ``/{slug}`` default above. Its old
``GET /se/{slug}/view3d`` URL still resolves —
a permanent (308) redirect to the new bare-slug URL, query string
forwarded unchanged (``_view3d_redirect``), so an old bookmark/link
never just 404s.

The anchored-notes layer arrived via
docs/backlog/se-topology-cloud-and-surface-notes.md slice 2:

* ``POST /se/{slug}/note/rewrite`` — AI-assisted intent capture: a raw
  comment typed against the 3D selection comes back as a crisp one-or-
  two-sentence note + a proposed kind (question | decision), for the
  page to show inline for accept/edit. LLM failure degrades to the raw
  text (``degraded: true``) — capture never blocks on the model.
* ``POST /se/{slug}/note`` — save the accepted note into the design's
  interrogation ledger via the existing ``add_note`` op path (origin
  ``'user'``, ``about=[block]``, verbatim original appended as a
  ``(verbatim: …)`` trailer when the text was rewritten).

The revision scrubber (the design-workbench build, slice 2 (2026-09-18)):
``?rev=N`` on the 3D page and on ``scene3d.json``. N < current renders
the tree from the ``rev-N`` ``design_checkpoints`` snapshot the handler
took at that save (:func:`precis_se.persist.tree_from_json`); the blocks
that differ from N−1 — added, or with a changed pose/rot/envelope/parent,
matched by block ``uid`` — come back as ``changed_uids`` and are tinted in
the Shapes tree (:func:`precis_web.blocktree_3d.tint_blocks`). The page's
"Revision N" panel lists that diff, the recorded ops and the chat ``turn``;
a past revision is read-only (no note panel). A design saved before the
record existed shows one position, "current, no record".

The design chat (slice 3, ``precis_web.design_chat`` + the shared
``_design_chat.html.j2`` partial): ``POST /se/{slug}/chat`` (form
``message`` + ``handles``) runs one tool-less :func:`design_turn.run_turn`
off the event loop and 303s back with the outcome in the query string;
``POST /se/{slug}/chat/apply`` (form ``ops`` JSON + ``turn``) is the human
Apply on a store-aware proposal. Both refuse a ``?rev=`` naming a past
revision (409) — a turn edits the current tree. The panel is hidden on a
past revision.

Still out of scope (see the gripe): argue-with-points (click → anchor →
job).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from precis.blocktree.types import BlockNode, Tree
from precis.design import history as design_history
from precis.dispatch import Hub
from precis.errors import NotFound
from precis.handlers._slug_ref_shared import resolve_live_slug_ref
from precis_se import persist as se_persist
from precis_se import stability as se_stability
from precis_se import validate as se_validate
from precis_se.ops import effective_envelope as se_effective_envelope
from precis_web import design_chat, design_turn
from precis_web.blocktree_3d import CHANGED_COLOUR, Scene3D, build_scene, tint_blocks
from precis_web.blocktree_svg import (
    AXES,
    COLOUR_CHANNELS,
    LEVELS,
    Axis,
    MemberLine,
    Tier,
    build_block_draws,
    children_map,
    fill_fraction_line,
    force_colour,
    plan_visibility,
    project_point,
    render_svg,
    validator_summary,
)
from precis_web.deps import get_store, templates
from precis_web.timefmt import ago as _ago

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

router = APIRouter(tags=["blocktree"])

#: List-view cap (a browse surface, not an export) — mirrors cad.py.
_LIST_LIMIT = 100

_TIER_RANK = {"ok": 0, "warn": 1, "error": 2}


def _combine_tier(a: Tier, b: Tier) -> Tier:
    return a if _TIER_RANK[a] >= _TIER_RANK[b] else b


def _stability_tier(verdict: str) -> Tier:
    """Stability-verdict → header tier.

    ``classify()``'s verdict strings carry dynamic counts (``"rigid
    (statically indeterminate — 2 self-stress state(s))"``), so this
    matches by prefix, never exact equality. Three buckets:

    * ``rigid (...)``, ``prestress-stabilized (...)``, ``no axial
      members ...`` — fine, tier 'ok'.
    * the tripwire line (:data:`precis_se.stability.TRIPWIRE_LINE`,
      verbatim) — an honest "the second-order test couldn't run", never
      a confirmed failure — tier 'warn'. The header still renders the
      line's full text (never a shortened label): a "not checked"
      statement earns the qualifier, not just the colour.
    * every other verdict — by construction (``stability._verdict``) that
      is one of the "first-order mobile ... NOT stabilized ..." /
      "... infeasible for the declared members ..." phrasings: a
      *confirmed* unstabilized mechanism — tier 'error'.
    """
    if verdict == se_stability.TRIPWIRE_LINE:
        return "warn"
    if (
        verdict.startswith("rigid")
        or verdict.startswith("prestress-stabilized")
        or verdict.startswith("no axial members")
    ):
        return "ok"
    return "error"


@dataclass
class _Adapter:
    kind: str
    label: str
    load_tree: Any  # Callable[[Store, int], Tree[BlockNode, Any]]
    effective_envelope: Any  # blocktree_svg.EffectiveEnvelopeFn, per the domain's own tree/block subclasses
    validate: Any  # Callable[[Tree[BlockNode, Any]], list[Any]]
    is_realized: Any  # Callable[[BlockNode], bool]
    has_stability: bool
    #: round 2a — the 3D/mermaid connectivity overlay's per-connect label
    #: (spec §5.8 comment 5(c)) and colour: se reads its ``joint`` dict
    #: and reuses the SVG force-colour vocabulary for kinematic connects.
    #: Per-adapter because a connect's L2 statement is domain vocabulary,
    #: not a shared-core field.
    connect_label: Any  # Callable[[Connect], str]
    connect_colour: Any  # Callable[[Connect], str]


def _se_is_realized(node: Any) -> bool:
    return bool(getattr(node, "mode", None)) or bool(getattr(node, "bound_kind", None))


def _se_connect_label(c: Any) -> str:
    joint = getattr(c, "joint", None) or {}
    return str(joint.get("mechanism") or joint.get("class") or "connect")


def _se_connect_colour(c: Any) -> str:
    joint = getattr(c, "joint", None) or {}
    return _ROLE_NEUTRAL if joint.get("class") != "axial" else _ROLE_TIE


def _se_member_facts(tree: Any) -> dict[str, dict[str, Any]]:
    """Per-member facts for the topology panel's hover, keyed by the
    stability report's ``subject`` (``a.port—b.port`` — the same key
    :attr:`~precis_web.blocktree_3d.ConnLine.subject` carries, so the
    client joins rather than parses).

    ONLY what a solve actually produced (spec slice 1's honesty rule —
    never invent a number): ``role`` and, when present, the skip reason,
    the normalized ``self_stress`` coefficient from
    :func:`~precis_se.stability.classify`, and the real newton figures
    (``declared_n``/``implied_n``) from
    :func:`~precis_se.stability.prestress_report` — which returns ``None``
    outright when no member declares a preload, so an un-prestressed
    design simply has no force numbers to show. A member with no entry at
    all (a non-axial connect) makes the client say so rather than print a
    zero."""
    facts: dict[str, dict[str, Any]] = {}
    report = se_stability.classify(tree)
    for row in report.members:
        entry: dict[str, Any] = {"role": row.role}
        if row.skipped is not None:
            entry["skipped"] = row.skipped
        elif row.self_stress is not None:
            entry["self_stress"] = float(row.self_stress)
        facts[row.subject] = entry
    prestress = se_stability.prestress_report(tree)
    if prestress is not None:
        for prow in prestress.rows:
            entry = facts.setdefault(prow.subject, {"role": prow.role})
            if prow.declared is not None:
                entry["declared_n"] = float(prow.declared)
            if prow.implied is not None:
                entry["implied_n"] = float(prow.implied)
    return facts


#: The 3D/mermaid connectivity overlay's own small colour vocabulary —
#: distinct from :func:`~precis_web.blocktree_svg.force_colour`'s tie/
#: strut/neutral triple (that one needs a computed self-stress sign this
#: overlay doesn't have; this is just "kinematic (se's axial) vs a plain
#: attachment").
_ROLE_TIE = "#16a34a"
_ROLE_NEUTRAL = "#64748b"


_ADAPTERS: dict[str, _Adapter] = {
    "se": _Adapter(
        kind="se",
        label="Structural envelopes",
        load_tree=se_persist.load_tree,
        effective_envelope=se_effective_envelope,
        validate=se_validate.validate,
        is_realized=_se_is_realized,
        has_stability=True,
        connect_label=_se_connect_label,
        connect_colour=_se_connect_colour,
    ),
}

#: One SQL statement per kind's own block table — the blocks' stable
#: ``uid``s, keyed by name, for the 3D route's leaf-id/mermaid-node-id
#: scheme (module docstring; :mod:`precis_web.blocktree_3d`'s "no lookup
#: table" pick convention still needs THIS one server-side mapping once
#: per render, since ``Tree.blocks`` itself is keyed by name — see
#: ``precis.blocktree.types.BlockNode``'s own docstring). The uid, not
#: ``se_blocks.id``: a row id is rebuilt by every save, so a viewer path
#: built from one would churn under an unrelated edit
#: (docs/backlog/design-state-core.md item 2).
_UID_SQL = {
    "se": "SELECT uid, name FROM se_blocks WHERE ref_id = %s AND retired_at IS NULL",
}


def _uid_by_name(store: Store, kind: str, ref_id: int) -> dict[str, int]:
    with store.pool.connection() as conn:
        rows = conn.execute(_UID_SQL[kind], (ref_id,)).fetchall()
    return {r[1]: int(r[0]) for r in rows}


#: One SQL statement per kind's own block table — table names are never
#: interpolated from a request, so this stays a plain literal rather than
#: a dynamic-identifier query.
_LIST_SQL = {
    "se": """
        SELECT r.ref_id,
               (SELECT id_value FROM ref_identifiers
                 WHERE ref_id = r.ref_id AND id_kind = 'cite_key'
                 ORDER BY created_at DESC LIMIT 1)             AS slug,
               r.title,
               (SELECT count(*) FROM se_blocks b
                 WHERE b.ref_id = r.ref_id AND b.retired_at IS NULL) AS n_blocks,
               r.updated_at
          FROM refs r
         WHERE r.kind = 'se' AND r.retired_at IS NULL
         ORDER BY r.ref_id DESC
         LIMIT %s
    """,
}


def _list_rows(store: Store, kind: str) -> list[dict[str, Any]]:
    with store.pool.connection() as conn:
        rows = conn.execute(_LIST_SQL[kind], (_LIST_LIMIT,)).fetchall()
    return [
        {
            "ref_id": int(r[0]),
            "slug": r[1],
            "title": r[2] or r[1],
            "n_blocks": int(r[3]),
            "updated": _ago(r[4]),
        }
        for r in rows
    ]


def _require_ref(store: Store, kind: str, slug: str) -> Any:
    return resolve_live_slug_ref(store, kind=kind, id=slug)


def _parse_overrides(raw: str) -> dict[str, str]:
    """``"name:level, name2:level2"`` -> ``{name: level, ...}`` (round
    2a's per-subtree level override, plain-GET-form-friendly per round
    1's own "no client JS needed" convention). A block name may itself
    contain ``':'`` (round 1's isolate already treats a block name as
    accepting almost any character bar ``'#'``), so each comma-separated
    segment splits on its LAST ``':'``, not its first. A malformed
    segment (no colon, or an empty name/level either side of it) is
    dropped rather than erroring the whole param — ``plan_visibility``
    itself is still the one authority on an unrecognised LEVEL name (it
    raises, mapped to a 400 same as an unknown ambient ``level``); an
    unrecognised BLOCK name is silently inert there too (this module's
    own docstring). CAVEAT: this splits on ``','`` FIRST, so — unlike
    ``isolate``, which promises "almost any character bar ``'#'``" for a
    block name — a block name containing a literal comma can never be
    targeted through this param (round 1's isolate docstring's promise
    doesn't extend here)."""
    out: dict[str, str] = {}
    for segment in raw.split(","):
        segment = segment.strip()
        if not segment or ":" not in segment:
            continue
        name, _, level = segment.rpartition(":")
        name, level = name.strip(), level.strip()
        if name and level:
            out[name] = level
    return out


def _valid_overrides_qs(raw: str, tree: Tree[BlockNode, Any]) -> str:
    """Re-serialize ``overrides`` down to entries whose name is a real
    block in THIS tree and whose level is a recognised ladder step —
    the same known-value allowlist ``axis``/``level``/``colour`` already
    get before a page embeds them in another URL. ``_view3d_page`` calls
    this before building ``scene_url``: an entry that fails either check
    is dropped rather than kept-but-flagged, so the string this page
    ever re-embeds (in the URL, and in the JSON error a bad ``overrides``
    could otherwise trigger downstream) can never carry arbitrary
    attacker text through to the browser."""
    parsed = _parse_overrides(raw)
    valid = {n: lv for n, lv in parsed.items() if n in tree.blocks and lv in LEVELS}
    return ",".join(f"{n}:{lv}" for n, lv in valid.items())


# ── revision scrubber (design-workbench build, slice 2) ──────────────────


class _NoSuchRevision(Exception):
    """``rev`` names a version this design never had, or whose snapshot is
    gone — the route turns it into a 404 with the message."""


@dataclass
class _RevisionAxis:
    """Where the scrubber stands: the newest recorded revision (``current``
    — 1 with no record, so the slider still has its one position), the
    revision on screen (``shown``), and whether that is a past one."""

    current: int
    shown: int
    has_record: bool

    @property
    def read_only(self) -> bool:
        return self.shown != self.current


def _revision_axis(store: Store, ref_id: int, rev: int | None) -> _RevisionAxis:
    """Resolve ``?rev=`` against the design's ``design_revisions`` rows.
    ``None`` is the current revision; anything outside ``1..current`` is a
    404 (never a clamp — a stale link to a version that never existed
    should say so, not silently show a different one)."""
    revisions = design_history.list_revisions(store, ref_id)
    current = max((r.rev for r in revisions), default=1)
    shown = current if rev is None else rev
    if not 1 <= shown <= current:
        raise _NoSuchRevision(f"no revision {shown} (current is {current})")
    return _RevisionAxis(current=current, shown=shown, has_record=bool(revisions))


def _snapshot_tree(store: Store, ref_id: int, rev: int) -> Tree[Any, Any] | None:
    """The tree as of save ``rev`` from its ``rev-<N>`` checkpoint, or
    ``None`` when no such snapshot exists (rev 0, a pre-record save, or a
    deleted checkpoint)."""
    if rev < 1:
        return None
    checkpoint = design_history.load_checkpoint(store, ref_id, f"rev-{rev}")
    if checkpoint is None:
        return None
    return se_persist.tree_from_json(checkpoint.payload, store=store)


def _tree_at(
    store: Store, kind: str, ref_id: int, axis: _RevisionAxis
) -> Tree[BlockNode, Any]:
    """The tree the page/scene shows: live for the current revision, the
    ``rev-N`` snapshot for a past one (a past revision with no snapshot is
    a 404 — there is nothing honest to draw)."""
    if not axis.read_only:
        tree: Tree[BlockNode, Any] = _ADAPTERS[kind].load_tree(store, ref_id)
        return tree
    snapshot = _snapshot_tree(store, ref_id, axis.shown)
    if snapshot is None:
        raise _NoSuchRevision(f"revision {axis.shown} has no snapshot to show")
    return snapshot


def _uids_of(tree: Tree[BlockNode, Any]) -> dict[str, int]:
    """``{name: uid}`` off the tree's own blocks — the snapshot path's
    replacement for :func:`_uid_by_name` (the live-row query would answer
    for today's blocks, not the snapshot's)."""
    return {
        name: int(uid)
        for name, node in tree.blocks.items()
        if (uid := getattr(node, "uid", None)) is not None
    }


#: The block fields whose change between two revisions marks the block as
#: "changed" in the scrubber — the L1 triple plus its place in the tree.
_BLOCK_DIFF_FIELDS = ("pose", "rot", "envelope", "parent", "name")


def _block_diff(
    prev: Tree[BlockNode, Any] | None, cur: Tree[BlockNode, Any]
) -> dict[str, Any]:
    """Blocks that differ between two revisions, matched by ``uid`` (the
    identity that survives a save — a rename is a change, not an add +
    remove). ``prev=None`` is "before the first save": everything is
    added. ``changed_uids`` is what the scene tints — the added and changed
    blocks, which exist at ``cur``; removed ones have nothing to tint."""
    before = {u: n for n, u in _uids_of(prev).items()} if prev is not None else {}
    after = {u: n for n, u in _uids_of(cur).items()}
    prev_blocks = prev.blocks if prev is not None else {}
    added = [{"uid": u, "name": after[u]} for u in sorted(after) if u not in before]
    removed = [{"uid": u, "name": before[u]} for u in sorted(before) if u not in after]
    changed: list[dict[str, Any]] = []
    for uid in sorted(after):
        if uid not in before:
            continue
        a, b = prev_blocks[before[uid]], cur.blocks[after[uid]]
        what = [f for f in _BLOCK_DIFF_FIELDS if getattr(a, f) != getattr(b, f)]
        if what:
            changed.append({"uid": uid, "name": after[uid], "what": what})
    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "changed_uids": [d["uid"] for d in added] + [d["uid"] for d in changed],
    }


def _revision_panel(
    store: Store, ref_id: int, axis: _RevisionAxis, tree: Tree[BlockNode, Any]
) -> dict[str, Any]:
    """The "Revision N" panel: the block diff against N−1, the recorded ops
    (one pretty-JSON line each) or "no op record", and the chat ``turn``.
    Reads only."""
    if axis.has_record:
        diff = _block_diff(_snapshot_tree(store, ref_id, axis.shown - 1), tree)
    else:
        diff = {"added": [], "removed": [], "changed": [], "changed_uids": []}
    record = design_history.revision(store, ref_id, axis.shown)
    return {
        "rev": axis.shown,
        "current": axis.current,
        "has_record": record is not None,
        "diff": diff,
        "n_blocks": len(tree.blocks),
        "ops": [
            json.dumps(op, sort_keys=True) for op in (record.ops if record else [])
        ],
        "turn": record.turn if record else None,
        "created": _ago(record.created_at) if record and record.created_at else None,
    }


def _changed_uids(
    store: Store, ref_id: int, axis: _RevisionAxis, tree: Any
) -> set[int]:
    """The uids the scene tints for revision N — empty for a design with
    no record (nothing to diff against)."""
    if not axis.has_record:
        return set()
    diff = _block_diff(_snapshot_tree(store, ref_id, axis.shown - 1), tree)
    return set(diff["changed_uids"])


async def _list_page(request: Request, kind: str) -> HTMLResponse:
    store = get_store(request)
    adapter = _ADAPTERS[kind]
    rows = _list_rows(store, kind)
    return templates.TemplateResponse(
        request,
        "blocktree/list.html.j2",
        {
            "active_tab": kind,
            "kind": kind,
            "kind_label": adapter.label,
            "designs": rows,
            "total": len(rows),
        },
    )


async def _detail_page(
    request: Request,
    kind: str,
    slug: str,
    *,
    axis: str,
    level: str,
    colour: str,
    isolate: str | None,
    overrides: str,
) -> Any:
    store = get_store(request)
    adapter = _ADAPTERS[kind]
    try:
        ref = _require_ref(store, kind, slug)
    except NotFound:
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {
                "title": f"{adapter.label} design not found",
                "detail": f"no live {kind} design with slug {slug!r}",
                "status": 404,
            },
            status_code=404,
        )
    tree = await asyncio.to_thread(adapter.load_tree, store, ref.id)
    block_names = sorted(tree.blocks)
    axis = axis if axis in AXES else "z"
    level = level if level in LEVELS else "refined"
    colour = colour if colour in COLOUR_CHANNELS else "part"
    isolate = isolate if isolate in tree.blocks else None
    # slug/isolate are design-controlled (block/slug names accept almost
    # any character) — url-encode both so a name like "fork & tip" or one
    # containing '#' can't corrupt/truncate the query string.
    common_qs = f"level={level}"
    if isolate:
        common_qs += f"&isolate={quote(isolate, safe='')}"
    if overrides.strip():
        common_qs += f"&overrides={quote(overrides, safe='')}"
    svg_url = f"/{kind}/{quote(slug, safe='')}/view.svg?axis={axis}&{common_qs}&colour={colour}"
    # gr337745: the 3D view is now the default landing page at the bare
    # slug URL — no '/view3d' suffix.
    view3d_url = f"/{kind}/{quote(slug, safe='')}?{common_qs}"
    return templates.TemplateResponse(
        request,
        "blocktree/detail.html.j2",
        {
            "active_tab": kind,
            "kind": kind,
            "kind_label": adapter.label,
            "slug": ref.slug,
            "title": ref.title or ref.slug,
            "axes": AXES,
            "levels": LEVELS,
            "colour_channels": COLOUR_CHANNELS,
            "axis": axis,
            "level": level,
            "colour": colour,
            "isolate": isolate or "",
            "overrides": overrides,
            "block_names": block_names,
            "svg_url": svg_url,
            "view3d_url": view3d_url,
        },
    )


def _plan_or_error(
    tree: Tree[BlockNode, Any],
    kids: dict[str, list[str]],
    *,
    level: str,
    isolate: str | None,
    level_overrides: dict[str, str],
) -> tuple[Any, str | None]:
    """``(plan, None)`` or ``(None, error message)`` — the one place both
    the SVG and 3D builders turn ``plan_visibility``'s two failure modes
    (unknown ``isolate`` name; an ``overrides`` entry naming an unknown
    LEVEL — an unknown BLOCK name in ``overrides`` is silently dropped by
    ``plan_visibility`` itself, never an error here) into a route-facing
    message."""
    try:
        plan = plan_visibility(
            tree, kids, level=level, isolate=isolate, level_overrides=level_overrides
        )
    except KeyError:
        return None, f"no such subtree {isolate!r}"
    except ValueError as exc:
        return None, str(exc)
    return plan, None


def _build_svg(
    store: Store,
    kind: str,
    ref_id: int,
    *,
    axis: Axis,
    level: str,
    colour: str,
    isolate: str | None,
    level_overrides: dict[str, str],
) -> tuple[str | None, str | None]:
    """Off the event loop (store round-trip + the tessellate/hull work).
    ``(None, error)`` when ``isolate``/``overrides`` names something bad —
    the route maps that to a 400, never a silent empty image."""
    adapter = _ADAPTERS[kind]
    tree: Tree[BlockNode, Any] = adapter.load_tree(store, ref_id)
    kids = children_map(tree)
    plan, err = _plan_or_error(
        tree, kids, level=level, isolate=isolate, level_overrides=level_overrides
    )
    if plan is None:
        return None, err
    draws = build_block_draws(
        tree,
        adapter.effective_envelope,
        kids,
        plan,
        axis,
        is_realized=adapter.is_realized,
    )
    fill_line = fill_fraction_line(tree, adapter.effective_envelope)
    findings = adapter.validate(tree)
    vline, vtier = validator_summary(findings)
    members: list[MemberLine] = []
    if adapter.has_stability:
        report = se_stability.classify(tree)  # type: ignore[arg-type]
        header_lines = [
            f"stability: {report.verdict}",
            f"validate: {vline}",
            fill_line,
        ]
        tier = _combine_tier(_stability_tier(report.verdict), vtier)
        for row in report.members:
            if row.skipped is not None:
                continue
            a = tree.blocks.get(row.a_block)
            b = tree.blocks.get(row.b_block)
            if a is None or b is None:
                continue
            members.append(
                MemberLine(
                    a=project_point(a.pose, axis),
                    b=project_point(b.pose, axis),
                    colour=force_colour(row.role, row.self_stress),
                    subject=row.subject,
                )
            )
    else:
        header_lines = [f"validate: {vline}", fill_line]
        tier = vtier
    svg = render_svg(
        draws, members, channel=colour, header_lines=header_lines, tier=tier
    )
    return svg, None


async def _svg_response(
    request: Request,
    kind: str,
    slug: str,
    *,
    axis: str,
    level: str,
    colour: str,
    isolate: str | None,
    overrides: str,
) -> Response:
    store = get_store(request)
    try:
        ref = _require_ref(store, kind, slug)
    except NotFound:
        return JSONResponse({"error": "not found"}, status_code=404)
    if axis not in AXES:
        return JSONResponse(
            {"error": f"unknown axis {axis!r} — one of {AXES}"}, status_code=400
        )
    if level not in LEVELS:
        return JSONResponse(
            {"error": f"unknown level {level!r} — one of {LEVELS}"}, status_code=400
        )
    if colour not in COLOUR_CHANNELS:
        return JSONResponse(
            {"error": f"unknown colour {colour!r} — one of {COLOUR_CHANNELS}"},
            status_code=400,
        )
    level_overrides = _parse_overrides(overrides)

    def _build() -> tuple[str | None, str | None]:
        return _build_svg(
            store,
            kind,
            ref.id,
            axis=axis,
            level=level,
            colour=colour,
            isolate=isolate,
            level_overrides=level_overrides,
        )

    svg, err = await asyncio.to_thread(_build)
    if svg is None:
        return JSONResponse({"error": err}, status_code=400)
    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={"Cache-Control": "no-store"},
    )


async def _view3d_page(
    request: Request,
    kind: str,
    slug: str,
    *,
    level: str,
    isolate: str | None,
    overrides: str,
    rev: int | None = None,
) -> Any:
    store = get_store(request)
    adapter = _ADAPTERS[kind]
    try:
        ref = _require_ref(store, kind, slug)
    except NotFound:
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {
                "title": f"{adapter.label} design not found",
                "detail": f"no live {kind} design with slug {slug!r}",
                "status": 404,
            },
            status_code=404,
        )

    def _load() -> tuple[_RevisionAxis, Tree[BlockNode, Any], dict[str, Any]]:
        axis = _revision_axis(store, ref.id, rev)
        tree = _tree_at(store, kind, ref.id, axis)
        return axis, tree, _revision_panel(store, ref.id, axis, tree)

    try:
        axis, tree, revision = await asyncio.to_thread(_load)
    except _NoSuchRevision as exc:
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {
                "title": f"{adapter.label} design {slug!r}: {exc}",
                "detail": f"design {slug!r} has {exc}",
                "status": 404,
            },
            status_code=404,
        )
    block_names = sorted(tree.blocks)
    level = level if level in LEVELS else "refined"
    isolate = isolate if isolate in tree.blocks else None
    # Validate BEFORE embedding anywhere downstream (scene_url, and from
    # there the scene3d.json fetch the 3D page's own JS makes) — an
    # unvalidated overrides string can otherwise ride through to a 400
    # JSON error that echoes an unrecognised LEVEL name verbatim, which
    # the page's own error handling used to render unescaped (reflected
    # DOM XSS; both ends are fixed — this validation, and the client no
    # longer using innerHTML for server-supplied text either).
    valid_overrides_qs = _valid_overrides_qs(overrides, tree)
    common_qs = f"level={level}"
    if isolate:
        common_qs += f"&isolate={quote(isolate, safe='')}"
    if valid_overrides_qs:
        common_qs += f"&overrides={quote(valid_overrides_qs, safe='')}"
    # The scene carries ``rev`` only when the reader asked for one — the
    # bare page stays the live tree, untinted, exactly as before.
    scene_qs = common_qs + (f"&rev={axis.shown}" if rev is not None else "")
    scene_url = f"/{kind}/{quote(slug, safe='')}/scene3d.json?{scene_qs}"
    # gr337745: the 2D SVG reader moved off the bare slug URL to '/2d'.
    detail_2d_url = f"/{kind}/{quote(slug, safe='')}/2d?{common_qs}"
    # Comment-on-selection (slice 2, se only today — the routes are
    # registered per kind, so a second blocktree kind opts in by adding
    # its own note routes; the template hides the panel when unset). A
    # past revision is read-only: the note op would land on the CURRENT
    # tree, so the panel is not offered at all.
    note_url = (
        f"/{kind}/{quote(slug, safe='')}/note"
        if kind == "se" and not axis.read_only
        else ""
    )
    note_rewrite_url = f"{note_url}/rewrite" if note_url else ""
    # The design chat (slice 3) — se only, like the note panel; on a past
    # revision the partial renders one read-only line and reads nothing.
    chat = (
        design_chat.panel_context(
            request,
            kind="se",
            slug=str(ref.slug),
            read_only=axis.read_only,
            rev_qs=f"?rev={axis.shown}" if rev is not None else "",
        )
        if kind == "se"
        else None
    )
    return templates.TemplateResponse(
        request,
        "blocktree/detail3d.html.j2",
        {
            "active_tab": kind,
            "kind": kind,
            "kind_label": adapter.label,
            "slug": ref.slug,
            "title": ref.title or ref.slug,
            "levels": LEVELS,
            "level": level,
            "isolate": isolate or "",
            "overrides": overrides,
            "block_names": block_names,
            "scene_url": scene_url,
            "detail_2d_url": detail_2d_url,
            "note_url": note_url,
            "note_rewrite_url": note_rewrite_url,
            # Scrubber state (slice 2): the revision on screen, the axis it
            # sits on, whether this is a read-only past view, and the panel.
            "rev": axis.shown,
            "rev_current": axis.current,
            "rev_explicit": rev is not None,
            "read_only": axis.read_only,
            "has_record": axis.has_record,
            "revision": revision,
            "chat": chat,
        },
    )


def _build_scene3d(
    store: Store,
    kind: str,
    ref_id: int,
    *,
    slug: str,
    level: str,
    isolate: str | None,
    level_overrides: dict[str, str],
    rev: int | None = None,
) -> tuple[Scene3D | None, dict[str, dict[str, Any]], list[int], str | None]:
    """Off the event loop, mirroring :func:`_build_svg`'s shape: the
    round-2a analogue building a :class:`~precis_web.blocktree_3d.Scene3D`
    off the SAME plan instead of an SVG string.

    Returns ``(scene, member facts, changed uids, error)`` — the facts
    (:func:`_se_member_facts`) are the topology panel's hover numbers,
    computed here rather than in the kind-agnostic scene builder because
    they are domain vocabulary (the same reason ``_build_svg`` calls
    ``se_stability`` directly). ``{}`` for a kind without a stability
    solve, or when the solve itself fails.

    ``rev`` (the scrubber) renders the ``rev-N`` snapshot instead of the
    live rows, with the blocks that changed between N−1 and N tinted
    (:func:`~precis_web.blocktree_3d.tint_blocks`) and their uids returned;
    ``None`` is the live tree, untinted, ``[]``."""
    adapter = _ADAPTERS[kind]
    changed: set[int] = set()
    if rev is None:
        tree: Tree[BlockNode, Any] = adapter.load_tree(store, ref_id)
        uid_by_name = _uid_by_name(store, kind, ref_id)
    else:
        axis = _revision_axis(store, ref_id, rev)
        tree = _tree_at(store, kind, ref_id, axis)
        # The snapshot's own uids, not today's rows — a block added since
        # would otherwise be looked up under the wrong identity.
        uid_by_name = _uids_of(tree)
        changed = _changed_uids(store, ref_id, axis, tree)
    kids = children_map(tree)
    plan, err = _plan_or_error(
        tree, kids, level=level, isolate=isolate, level_overrides=level_overrides
    )
    if plan is None:
        return None, {}, [], err
    scene = build_scene(
        tree,
        adapter.effective_envelope,
        kids,
        plan,
        uid_by_name,
        # gr338445: the slug, not the numeric ref id — a viewer path like
        # ``/se-337761/_connections`` is opaque; ``/se-<slug>/_connections``
        # tells the reader what they're looking at.
        root_id=f"/{kind}-{slug}",
        root_name=adapter.label,
        label_fn=adapter.connect_label,
        colour_fn=adapter.connect_colour,
    )
    tint_blocks(scene.shapes, changed, CHANGED_COLOUR)
    facts: dict[str, dict[str, Any]] = {}
    if adapter.has_stability:
        try:
            facts = _se_member_facts(tree)
        except Exception:
            # Hover enrichment must never 500 the scene: no numbers is an
            # honest degrade (the tooltip then says there is no solve),
            # a broken topology panel is not.
            log.exception("member facts failed for %s %s", kind, slug)
    return scene, facts, sorted(changed), None


async def _scene3d_response(
    request: Request,
    kind: str,
    slug: str,
    *,
    level: str,
    isolate: str | None,
    overrides: str,
    rev: int | None = None,
) -> Response:
    store = get_store(request)
    try:
        ref = _require_ref(store, kind, slug)
    except NotFound:
        return JSONResponse({"error": "not found"}, status_code=404)
    if level not in LEVELS:
        return JSONResponse(
            {"error": f"unknown level {level!r} — one of {LEVELS}"}, status_code=400
        )
    level_overrides = _parse_overrides(overrides)

    def _build() -> tuple[
        Scene3D | None, dict[str, dict[str, Any]], list[int], str | None
    ]:
        return _build_scene3d(
            store,
            kind,
            ref.id,
            # ref.slug (not the raw path param) — the canonical slug even
            # if the URL was addressed by some other resolvable id; a
            # blocktree kind is always slug-addressed in practice, but
            # ``Ref.slug`` types as Optional, so fall back to the path
            # param on the defensive ``None`` case.
            slug=ref.slug or slug,
            level=level,
            isolate=isolate,
            level_overrides=level_overrides,
            rev=rev,
        )

    try:
        scene, facts, changed_uids, err = await asyncio.to_thread(_build)
    except _NoSuchRevision as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    if scene is None:
        return JSONResponse({"error": err}, status_code=400)
    return JSONResponse(
        {
            "shapes": scene.shapes,
            # The scrubber's diff (slice 2): blocks added or changed at
            # ``rev`` vs the revision before, by uid — the same uids the
            # tinted leaves' paths end in. ``[]`` for the live scene.
            "changed_uids": changed_uids,
            "connections": [
                {
                    "path": c.path,
                    "a_name": c.a_name,
                    "b_name": c.b_name,
                    "a_path": c.a_path,
                    "b_path": c.b_path,
                    "label": c.label,
                    "colour": c.colour,
                    # The topology cloud's join key into ``forces`` below,
                    # and its hover "gap" line.
                    "subject": c.subject,
                    "witness_gap": c.witness_gap,
                }
                for c in scene.connections
            ],
            "explode": scene.explode,
            # The topology cloud's own input (slice 1): the visible blocks,
            # parent-linked, with whatever detail the tree declared.
            "nodes": [
                {
                    "id": n.id,
                    "name": n.name,
                    "path": n.path,
                    "parent": n.parent,
                    "kind": n.kind,
                    "detail": n.detail,
                }
                for n in scene.nodes
            ],
            #: Per-member force facts keyed by connect ``subject`` — empty
            #: when no solve produced any (never a fabricated zero).
            "forces": facts,
            "mermaid": scene.mermaid,
            # gr340030 — the scale-bar overlay's own conversion factor:
            # real SI metres = a displayed coordinate / scale.
            "scale": scene.scale,
        },
        headers={"Cache-Control": "no-store"},
    )


async def _view3d_redirect(request: Request, kind: str, slug: str) -> Response:
    """gr337745 moved the 3D view off ``/{kind}/{slug}/view3d`` onto the
    bare slug URL — a PERMANENT redirect (308, GET-only so 307 vs 308
    behave identically here) keeps any old bookmark/link working rather
    than 404ing outright. Forwards the query string (``level``/
    ``isolate``/``overrides``) unchanged; it's already percent-encoded
    off the incoming request, so no re-encoding needed."""
    qs = request.url.query
    target = f"/{kind}/{quote(slug, safe='')}" + (f"?{qs}" if qs else "")
    return RedirectResponse(target, status_code=308)


# ── se routes ────────────────────────────────────────────────────────────


@router.get("/se", response_class=HTMLResponse)
async def se_list(request: Request) -> HTMLResponse:
    return await _list_page(request, "se")


@router.get("/se/{slug}")
async def se_detail(
    request: Request,
    slug: str,
    level: str = "refined",
    isolate: str | None = None,
    overrides: str = "",
    rev: int | None = None,
) -> Any:
    # gr337745: the 3D view is the default landing page now.
    return await _view3d_page(
        request, "se", slug, level=level, isolate=isolate, overrides=overrides, rev=rev
    )


@router.get("/se/{slug}/view3d")
async def se_view3d_redirect(request: Request, slug: str) -> Response:
    return await _view3d_redirect(request, "se", slug)


@router.get("/se/{slug}/2d")
async def se_detail_2d(
    request: Request,
    slug: str,
    axis: str = "z",
    level: str = "refined",
    colour: str = "part",
    isolate: str | None = None,
    overrides: str = "",
) -> Any:
    return await _detail_page(
        request,
        "se",
        slug,
        axis=axis,
        level=level,
        colour=colour,
        isolate=isolate,
        overrides=overrides,
    )


@router.get("/se/{slug}/view.svg")
async def se_view_svg(
    request: Request,
    slug: str,
    axis: str = "z",
    level: str = "refined",
    colour: str = "part",
    isolate: str | None = None,
    overrides: str = "",
) -> Response:
    return await _svg_response(
        request,
        "se",
        slug,
        axis=axis,
        level=level,
        colour=colour,
        isolate=isolate,
        overrides=overrides,
    )


@router.get("/se/{slug}/scene3d.json")
async def se_scene3d(
    request: Request,
    slug: str,
    level: str = "refined",
    isolate: str | None = None,
    overrides: str = "",
    rev: int | None = None,
) -> Response:
    return await _scene3d_response(
        request, "se", slug, level=level, isolate=isolate, overrides=overrides, rev=rev
    )


# ── comment-on-selection → interview note (slice 2 of
#    docs/backlog/se-topology-cloud-and-surface-notes.md) ─────────────────

#: The kinds the web comment box can mint. An ``answer`` needs a ``re``
#: target picked from the existing ledger — out of scope for a viewer
#: comment; the interview view is where answers happen.
_WEB_NOTE_KINDS = ("question", "decision")

_NOTE_REWRITE_PROMPT = """\
You are capturing design intent on a structural design. The user selected
block {block} in a 3D viewer and typed a raw comment about it. Rewrite the
comment as ONE crisp interview note of one or two sentences, preserving the
user's intent precisely — do not invent facts, soften, or expand scope.
Classify it: "question" if it asks or opens something, "decision" if it
settles or directs something.

Raw comment:
{comment}

Reply with ONLY a JSON object: {{"text": "...", "kind": "question" or "decision"}}
"""


def _rewrite_note_comment(comment: str, block: str | None) -> dict[str, Any]:
    """One MEDIUM-tier judge call: raw comment → ``{'text', 'kind'}``.
    Raises on any transport/parse failure — the route degrades to the raw
    text, this helper never does."""
    from precis.utils.llm.json_reply import extract_json_object
    from precis.utils.llm.router import LlmRequest, Tier, route

    prompt = _NOTE_REWRITE_PROMPT.format(
        block=repr(block) if block else "(none selected)", comment=comment
    )
    res = route(
        LlmRequest(
            tier=Tier.MEDIUM,
            source="se-note-rewrite",
            prompt=prompt,
            max_usd=0.10,
            timeout_s=60.0,
        )
    )
    if res.error:
        raise RuntimeError(res.error)
    data = res.data or extract_json_object(res.text) or {}
    text = str(data.get("text") or "").strip()
    if not text:
        raise RuntimeError("rewrite returned no text")
    kind = str(data.get("kind") or "").strip().lower()
    return {"text": text, "kind": kind if kind in _WEB_NOTE_KINDS else "question"}


def _note_name(kind: str, text: str, taken: set[str]) -> str:
    """A short, unique, readable ledger name for a web-minted note —
    ``q-``/``d-`` prefix (matching the hand-written ``q-bore`` style the
    handler's own examples teach) + the first few words, deduped with a
    numeric suffix. Note names are unique per design (``_op_add_note``)."""
    words = re.findall(r"[a-z0-9]+", text.lower())[:4]
    base = f"{'q' if kind == 'question' else 'd'}-{'-'.join(words) or 'note'}"
    name = base
    n = 2
    while name in taken:
        name = f"{base}-{n}"
        n += 1
    return name


@router.post("/se/{slug}/note/rewrite")
async def se_note_rewrite(request: Request, slug: str) -> JSONResponse:
    store = get_store(request)
    try:
        _require_ref(store, "se", slug)
    except NotFound:
        return JSONResponse({"error": f"no live se design {slug!r}"}, status_code=404)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "body must be JSON"}, status_code=400)
    comment = str(payload.get("comment") or "").strip()
    if not comment:
        return JSONResponse({"error": "empty comment"}, status_code=400)
    block = str(payload.get("block") or "").strip() or None
    try:
        proposal = await asyncio.to_thread(_rewrite_note_comment, comment, block)
    except Exception:
        # Degrade honestly: the raw words come back editable, flagged —
        # capture must never block on the model being reachable.
        log.exception("se note rewrite failed for %s", slug)
        return JSONResponse({"text": comment, "kind": "question", "degraded": True})
    return JSONResponse({**proposal, "degraded": False})


@router.post("/se/{slug}/note")
async def se_note_save(request: Request, slug: str) -> JSONResponse:
    store = get_store(request)
    try:
        ref = _require_ref(store, "se", slug)
    except NotFound:
        return JSONResponse({"error": f"no live se design {slug!r}"}, status_code=404)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "body must be JSON"}, status_code=400)
    text = str(payload.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "empty note text"}, status_code=400)
    kind = str(payload.get("kind") or "question").strip().lower()
    if kind not in _WEB_NOTE_KINDS:
        return JSONResponse(
            {"error": f"kind must be one of {' | '.join(_WEB_NOTE_KINDS)}"},
            status_code=400,
        )
    block = str(payload.get("block") or "").strip() or None
    verbatim = str(payload.get("verbatim") or "").strip()

    def _save() -> str:
        from precis_se.handler import SeHandler

        tree = se_persist.load_tree(store, ref.id)
        name = _note_name(kind, text, {n.name for n in tree.notes})
        body = text
        if verbatim and verbatim != text:
            body += f"\n\n(verbatim: {verbatim})"
        op: dict[str, Any] = {
            "op": "add_note",
            "name": name,
            "kind": kind,
            "text": body,
            "origin": "user",
        }
        if block:
            # A dangling anchor is legal by design (the interview view
            # annotates it) — no existence check here.
            op["about"] = [block]
        SeHandler(hub=Hub(store=store)).edit(id=str(ref.slug), ops=[op])
        return name

    try:
        name = await asyncio.to_thread(_save)
    except Exception as exc:
        # OpError/BadInput text is the actionable message; the client
        # renders it as textContent, never markup.
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "name": name})


# ── design chat (the design-workbench build, slice 3 (2026-09-18)) ───────────


def _chat_read_only(store: Store, ref_id: int, rev: int | None) -> str | None:
    """The refusal message when ``rev`` names a past revision (a turn or an
    Apply edits the CURRENT tree, never the one on screen), else ``None``."""
    if rev is None:
        return None
    try:
        axis = _revision_axis(store, ref_id, rev)
    except _NoSuchRevision as exc:
        return str(exc)
    if axis.read_only:
        return (
            f"revision {rev} is a past revision (current is {axis.current}) — "
            "chat on the current revision"
        )
    return None


@router.post("/se/{slug}/chat")
async def se_chat(
    request: Request,
    slug: str,
    message: str = Form(""),
    handles: str = Form(""),
    rev: int | None = None,
) -> Any:
    store = get_store(request)
    try:
        ref = _require_ref(store, "se", slug)
    except NotFound:
        return design_chat.chat_error(
            request,
            kind="se",
            slug=slug,
            detail=f"no live se design {slug!r}",
            status=404,
        )
    if not message.strip():
        return design_chat.chat_error(
            request, kind="se", slug=slug, detail="empty message", status=400
        )
    refusal = _chat_read_only(store, ref.id, rev)
    if refusal is not None:
        return design_chat.chat_error(
            request, kind="se", slug=slug, detail=refusal, status=409
        )
    hub = design_chat.hub_for(request)
    clicked = design_chat.parse_handles(handles)

    def _run() -> design_turn.TurnResult:
        return design_turn.run_turn(
            hub, kind="se", slug=str(ref.slug), message=message, handles=clicked
        )

    result = await asyncio.to_thread(_run)
    return design_chat.redirect_after("se", str(ref.slug), result)


@router.post("/se/{slug}/chat/apply")
async def se_chat_apply(
    request: Request,
    slug: str,
    ops: str = Form(""),
    turn: str = Form(""),
    rev: int | None = None,
) -> Any:
    store = get_store(request)
    try:
        ref = _require_ref(store, "se", slug)
    except NotFound:
        return design_chat.chat_error(
            request,
            kind="se",
            slug=slug,
            detail=f"no live se design {slug!r}",
            status=404,
        )
    parsed, err = design_chat.parse_ops_form(ops)
    if parsed is None:
        return design_chat.chat_error(
            request, kind="se", slug=slug, detail=err or "bad ops", status=400
        )
    refusal = _chat_read_only(store, ref.id, rev)
    if refusal is not None:
        return design_chat.chat_error(
            request, kind="se", slug=slug, detail=refusal, status=409
        )
    hub = design_chat.hub_for(request)

    def _apply() -> design_turn.TurnResult:
        return design_turn.apply_proposal(
            hub, kind="se", slug=str(ref.slug), ops=parsed, turn=turn.strip() or None
        )

    result = await asyncio.to_thread(_apply)
    return design_chat.redirect_after_apply("se", str(ref.slug), result)
