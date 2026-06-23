"""Target parser for the agent-facing ``link=`` / ``unlink=`` kwargs.

Canonical syntax (one form, no aliases):

    kind:identifier[~selector]

Examples:

    paper:wang2020state              # paper, ref-level
    paper:wang2020state~38           # paper, block at numeric pos 38
    paper:wang2020state~agenda       # paper, block whose slug is 'agenda'
    todo:158                         # todo (numeric kind), id=158
    memory:42                        # memory, id=42
    markdown:notes/meeting.md        # markdown, ref-level
    markdown:notes/meeting.md~intro  # markdown, block by slug

The previous (deferred) docs allowed bare slugs and the
``target:relation`` colon-suffix. We rejected both: bare slugs
required guesswork (which kind?), and overloading ``:`` between the
kind separator and the relation suffix made the grammar fragile
(any future relation slug containing ``:`` would silently change
parsing). The runtime now requires the prefix and takes the
relation as an explicit ``rel=`` kwarg.

The parser resolves the target by hitting the store. We do this
at parse time (not at INSERT time) so the agent gets a clear
"target not found" error rather than a foreign-key violation
buried in a stack trace.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from precis.errors import BadInput, NotFound, Unsupported
from precis.utils import handle_registry

if TYPE_CHECKING:
    from precis.store import Store


@dataclass(frozen=True, slots=True)
class LinkTarget:
    """Resolved target for a link= / unlink= kwarg.

    ``ref_id`` is the row id in ``refs``. ``pos`` is None for
    ref-level links (the common case) or an int for block-level
    links. Callers pass ``pos`` straight to
    :meth:`precis.store.Store.add_link` / ``remove_link``, which
    apply the -1 sentinel internally.
    """

    ref_id: int
    pos: int | None
    kind: str  # echoed back for hint construction in error paths
    raw: str  # original input string, for diagnostics


def parse_link_target(target: str, *, store: Store) -> LinkTarget:
    """Resolve ``'kind:identifier[~selector]'`` to a :class:`LinkTarget`.

    Raises:
        BadInput: malformed string (no colon, empty kind/identifier,
                  unknown kind, non-numeric id for a numeric kind).
        NotFound: the kind/identifier resolves to no live ref, or
                  the block selector resolves to no block.
    """
    if not isinstance(target, str) or not target.strip():
        raise BadInput(
            "link target must be a non-empty string",
            next="link='kind:identifier' (e.g. 'paper:wang2020state')",
        )

    # ADR 0036: accept a bare universal handle (``pc40``, ``me73``, …) —
    # the form search/list output now emits — and resolve it straight to a
    # LinkTarget so a handle copied out of a result round-trips into
    # link= / unlink= / like= without the agent re-deriving ``kind:slug~pos``.
    # Only fires for a well-formed refs-backed handle; everything else (incl.
    # the canonical ``kind:slug`` form, whose ``:`` defeats the handle parse)
    # falls through to the legacy grammar below untouched.
    handle = target.strip()
    if handle_registry.parse(handle) is not None:
        resolved = store.resolve_handle(handle)
        if resolved is None:
            raise NotFound(
                f"link target {handle!r} resolves to no live ref",
                next=f"check it exists: get(id={handle!r})",
            )
        return LinkTarget(
            ref_id=resolved.ref_id,
            pos=resolved.chunk_ord,
            kind=resolved.kind,
            raw=target,
        )

    if ":" not in target:
        raise BadInput(
            f"link target {target!r} missing required 'kind:' prefix",
            next=(
                "use canonical 'kind:identifier' form "
                "(e.g. 'paper:wang2020state' or 'todo:158')"
            ),
        )

    kind, _, rest = target.partition(":")
    kind = kind.strip()
    if not kind:
        raise BadInput(
            f"link target {target!r} has empty kind",
            next="link='kind:identifier'",
        )
    if not rest:
        raise BadInput(
            f"link target {target!r} has empty identifier after ':'",
            next="link='kind:identifier'",
        )

    # ``skill`` lives in package data (markdown on disk), not the
    # refs table. The skill body IS embedded (FileCorpusIndex carries
    # the vectors for semantic search), but linking requires a
    # ``refs.ref_id`` to populate ``links.src_ref_id`` / ``dst_ref_id``.
    # Pre-broad-pass behaviour was a misleading
    # NotFound("no live skill ref"); now Unsupported with the right
    # reason so the agent stops retrying and routes to a kind that
    # has rows. Broad-pass finding #6.
    if kind == "skill":
        raise Unsupported(
            f"link target {target!r}: skill is not linkable — "
            "served from package data, not the refs table",
            next=(
                "to anchor a thought to a skill, write a memory citing "
                "the skill's id in text and link memory→<the linkable "
                "target>"
            ),
        )

    # Validate kind against the live kinds table. We intentionally
    # do *not* gate on the registry (which only contains *active*
    # kinds in this build) — the schema may have a kind row that
    # has no handler yet (file kinds in 0004). Linking to such a
    # ref is fine; only handler-driven verbs care about the
    # registry.
    is_numeric = _kind_is_numeric(kind, store=store)

    # Split off the optional block selector.  The selector may
    # itself contain '~' (unlikely but legal in slugs), but the
    # store never produces such block slugs, and we'd rather
    # reject ambiguity than guess.
    if "~" in rest:
        identifier, _, selector = rest.partition("~")
        if not selector:
            raise BadInput(
                f"link target {target!r} has empty block selector after '~'",
                next="drop the trailing '~' for ref-level, or add a pos/slug",
            )
    else:
        identifier = rest
        selector = ""

    if not identifier:
        raise BadInput(
            f"link target {target!r} has empty identifier",
            next="link='kind:identifier'",
        )

    # Resolve the ref. Numeric kinds take int(refs.id); slug kinds
    # take refs.slug. ``store.get_ref`` returns None on miss; we
    # convert that to NotFound with the original input echoed back.
    ref_id_or_slug: int | str
    if is_numeric:
        try:
            ref_id_or_slug = int(identifier)
        except ValueError as exc:
            raise BadInput(
                f"kind {kind!r} is numeric - identifier must be an integer, "
                f"got {identifier!r}",
                next=f"link='{kind}:42' (numeric id)",
            ) from exc
    else:
        ref_id_or_slug = identifier

    ref = store.get_ref(kind=kind, id=ref_id_or_slug)
    if ref is None:
        raise NotFound(
            f"link target {target!r} resolves to no live {kind} ref",
            next=(
                f"check it exists: get(kind={kind!r}, id={identifier!r})"
                if not is_numeric
                else f"check it exists: get(kind={kind!r}, id={identifier})"
            ),
        )

    # Resolve the block selector, if any. Two forms: numeric pos
    # ('38') or block slug ('agenda'). We try numeric first since
    # slug kinds may have purely numeric block slugs in principle,
    # but in practice block slugs are content-derived hashes and
    # numeric pos is the dominant case.
    if not selector:
        return LinkTarget(ref_id=ref.id, pos=None, kind=kind, raw=target)

    pos: int | None = None
    if selector.isdigit() or (selector.startswith("-") and selector[1:].isdigit()):
        # Numeric pos. Negative pos has no meaning (the schema's
        # CHECK constraint enforces pos >= -1, and we never
        # accept -1 from the agent — that's our internal
        # ref-level sentinel). Reject anything < 0.
        candidate = int(selector)
        if candidate < 0:
            raise BadInput(
                f"link target {target!r} has negative block pos {candidate}",
                next="block positions are zero-indexed non-negative ints",
            )
        block = store.get_block(ref.id, pos=candidate)
        if block is None:
            raise NotFound(
                f"link target {target!r} - no block at pos={candidate} in {kind}:{identifier}",
                next=f"check available blocks: get(kind={kind!r}, id={identifier!r})",
            )
        pos = candidate
    else:
        # Block slug. Block slugs are unique within a ref
        # (UNIQUE constraint on `(ref_id, slug)`).
        block = store.get_block(ref.id, slug=selector)
        if block is None:
            raise NotFound(
                f"link target {target!r} - no block with slug={selector!r} "
                f"in {kind}:{identifier}",
                next=f"check available blocks: get(kind={kind!r}, id={identifier!r})",
            )
        pos = block.pos

    return LinkTarget(ref_id=ref.id, pos=pos, kind=kind, raw=target)


def _kind_is_numeric(kind: str, *, store: Store) -> bool:
    """Look up `kinds.is_numeric` for the given kind slug.

    Raises BadInput with the available options if the kind is not
    registered. Encapsulated here so the parser stays a single
    flat function from the caller's perspective.

    The "available options" list filters to kinds that actually
    have at least one live ref — the realistic link targets in
    this build. The ``kinds`` schema table also carries entries
    for handler-less kinds (file kinds whose env-gated handler
    isn't loaded), and surfacing them led an agent into a
    contradiction loop where the link error advertised
    ``markdown`` but ``get(kind='markdown', ...)`` then errored
    with ``unknown kind`` (MCP critic MAJOR-C 2026-05-02).
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT is_numeric FROM kinds WHERE slug = %s", (kind,)
        ).fetchone()
        if row is not None:
            return bool(row[0])
        # Bad kind — enumerate plausible link targets for the agent.
        # "Plausible" = kinds with at least one live ref. Empty corpora
        # fall back to the registered-kinds list so a fresh install
        # still gives the agent a non-empty hint.
        options = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT k.slug FROM kinds k "
                "JOIN refs r ON r.kind = k.slug "
                "WHERE r.deleted_at IS NULL "
                "ORDER BY k.slug"
            ).fetchall()
        ]
        if not options:
            options = [
                r[0]
                for r in conn.execute("SELECT slug FROM kinds ORDER BY slug").fetchall()
            ]
    raise BadInput(
        f"unknown kind {kind!r} in link target",
        options=options,
        next="check the kind name; link='kind:identifier'",
    )


__all__ = ["LinkTarget", "parse_link_target"]
