"""The angle spray and the dreamable-region view (docs/design/dreaming.md).

``AngleMixin`` carries ``search(angle=... | like=...)`` — the diverse-cone
semantic sampler — and ``search(view='dreamable')`` — the salience-seed
focus region. Both pick their own seed and cross-kind target set rather
than ranking a ``q=`` query the way :mod:`precis.runtime.search` does, and
both share the target-kind resolution and chunk-preview helpers below.
"""

from __future__ import annotations

from typing import Any

from precis.errors import BadInput, Unsupported, Upstream
from precis.response import Response
from precis.runtime._shared import CROSS_KIND_ALIASES as _CROSS_KIND_ALIASES
from precis.runtime._shared import RuntimeShape
from precis.utils.search_merge import SearchHit, merge_and_render

#: Preview length for an angle-spray hit. Short — the spray is a
#: breadth-first scan, not a read; the agent drills with ``get`` once a
#: neighbour looks worth chasing.
_ANGLE_PREVIEW_CHARS = 200


def _angle_excerpt(text: str) -> str:
    """One-line, length-capped preview of a snapped chunk's text."""
    flat = " ".join((text or "").split())
    if len(flat) <= _ANGLE_PREVIEW_CHARS:
        return flat
    return flat[: _ANGLE_PREVIEW_CHARS - 1].rstrip() + "…"


class AngleMixin(RuntimeShape):
    """The ``angle=``/``like=`` spray and the ``view='dreamable'`` region."""

    # Default dream-target kinds for the angle spray when the caller
    # doesn't pin one (docs/design/dreaming.md, §Scope). ``draft`` is
    # in here so the dreamer wanders the project write-up we're actively
    # building — the live prose we think about most, not just the frozen
    # corpus (paper) and crystallised thoughts (memory).
    _ANGLE_DEFAULT_KINDS: tuple[str, ...] = ("paper", "memory", "draft")

    # Focus-region size when the caller doesn't pin ``n=`` — wide
    # enough to read a theme, small enough to stay in one prompt.
    _DREAMABLE_DEFAULT_N: int = 12

    def _dispatch_dreamable(self, kind: Any, args: dict[str, Any]) -> Response:
        """The focus region: the salience seed + its ANN neighbourhood.

        ``search(view='dreamable')`` — pick the most-due seed
        (``argmax(last_seen - last_dreamt)`` over target kinds), return
        its nearest neighbourhood, and **stamp ``last_dreamt`` on every
        surfaced chunk** so the region rotates out and a different one
        tops the next run (docs/design/dreaming.md, §view='dreamable').
        No sub-clustering — the cosine ring *is* the region. Unlike the
        angle spray this does **not** bump salience: looking at a region
        counts as *dreaming* it, not as an external access.
        """
        store = self.hub.store
        if store is None:
            raise Unsupported("view='dreamable' needs a store-backed deployment")

        n = int(args.get("n") or self._DREAMABLE_DEFAULT_N)
        if n < 1:
            raise BadInput("n must be >= 1", next="search(view='dreamable', n=12)")

        kinds = self._angle_target_kinds(kind)
        seed_id, region = store.dreamable_region(kinds=kinds, n=n)

        # The rotation: surfacing a region IS dreaming it. Stamp the
        # seed and every surfaced chunk so the next run picks elsewhere.
        touched = [block.id for block, _ref, _score in region]
        if seed_id is not None and seed_id not in touched:
            touched.append(seed_id)
        if touched:
            store.touch_last_dreamt(touched)

        stream = [
            SearchHit(
                score=cosine,
                kind=ref.kind,
                title=ref.title or (ref.slug or f"#{ref.id}"),
                preview=_angle_excerpt(block.text),
                slug=ref.slug,
                pos=block.pos if block.pos is not None and block.pos >= 0 else None,
                ref_id=ref.id,
                dedupe_key=f"{ref.kind}:{block.id}",
            )
            for block, ref, cosine in region
        ]
        # ``query=`` populates the rendered header (``... for 'X'``);
        # passing the literal view name read as if the agent had typed
        # ``q='dreamable'`` (broad-pass usability finding R2#3). Use a
        # label that names what the view actually picked.
        seed_label = (
            f"most-due seed ref_id={seed_id}"
            if seed_id is not None
            else "most-due seed"
        )
        return merge_and_render(
            [stream],
            page_size=n,
            query=seed_label,
            header_noun="region member",
            mode="priority",
            show_label=True,
            empty_body="no dreamable region — corpus has no embedded target chunks yet",
        )

    def _dispatch_angle(self, kind: Any, args: dict[str, Any]) -> Response:
        """Diverse-cone semantic spray: ``n`` items at cosine ``angle``.

        ``search(q=... | like=<id>, angle=<float>, n=<int>)`` — seed by
        a query string or an existing item's stored vector, then return
        ``n`` mutually-distinct items at the requested cosine from the
        seed (docs/design/dreaming.md, §The ``angle`` spray). Card
        chunks are valid snap targets so a memory's only embedding is
        reachable. Surfacing bumps salience (suppressed for the dreamer).
        """
        store = self.hub.store
        if store is None:
            raise Unsupported("angle search needs a store-backed deployment")

        angle = self._angle_float(args.get("angle", 1.0))
        n = int(args.get("n") or args.get("top_k") or 8)
        if n < 1:
            raise BadInput("n must be >= 1", next="search(q='...', angle=0.5, n=8)")

        kinds = self._angle_target_kinds(kind)
        seed_vec, seed_chunk_id, label = self._resolve_angle_seed(
            args.get("like"), args.get("q")
        )

        exclude = [seed_chunk_id] if seed_chunk_id is not None else None
        hits = store.angle_neighbours(
            seed_vec, angle=angle, n=n, kinds=kinds, exclude_chunk_ids=exclude
        )
        # Surfacing is an external access → heat the snapped chunks
        # (no-op inside as_dream_actor); the dreamer stamps last_dreamt
        # itself at run end.
        store.bump_salience([block.id for block, _ref, _score in hits])

        stream = [
            SearchHit(
                score=cosine,
                kind=ref.kind,
                title=ref.title or (ref.slug or f"#{ref.id}"),
                preview=_angle_excerpt(block.text),
                slug=ref.slug,
                pos=block.pos if block.pos is not None and block.pos >= 0 else None,
                ref_id=ref.id,
                dedupe_key=f"{ref.kind}:{block.id}",
            )
            for block, ref, cosine in hits
        ]
        return merge_and_render(
            [stream],
            page_size=n,
            query=label,
            header_noun="neighbour",
            mode="priority",
            show_label=True,
            empty_body=f"no neighbours at angle={angle:g} from {label}",
        )

    @staticmethod
    def _angle_float(raw: Any) -> float:
        try:
            angle = float(raw)
        except (TypeError, ValueError) as exc:
            raise BadInput(
                f"angle must be a number in [-1, 1], got {raw!r}",
                next="search(q='...', angle=0.5)  # 1=near, 0=orthogonal, -1=opposite",
            ) from exc
        if not -1.0 <= angle <= 1.0:
            raise BadInput(
                f"angle must be in [-1, 1], got {angle}",
                next="1=same direction, 0=orthogonal, -1=opposite pole",
            )
        return angle

    def _angle_target_kinds(self, kind: Any) -> tuple[str, ...]:
        """Resolve the snap-target kinds for an angle spray.

        ``None`` / ``'*'`` / ``'all'`` → the default dream targets
        (paper+memory). A comma-list or single kind is honoured as-is
        so a caller can spray within one corpus.
        """
        if kind is None:
            return self._ANGLE_DEFAULT_KINDS
        if str(kind).strip().lower() in _CROSS_KIND_ALIASES:
            return self._ANGLE_DEFAULT_KINDS
        parsed = tuple(tok.strip() for tok in str(kind).split(",") if tok.strip())
        return parsed or self._ANGLE_DEFAULT_KINDS

    def _resolve_angle_seed(
        self, like: Any, q: Any
    ) -> tuple[list[float], int | None, str]:
        """Return ``(seed_vec, seed_chunk_id, label)`` for the spray.

        ``like='kind:id[~sel]'`` seeds from an existing item's stored
        vector (ref-level → its card/head chunk; block-level → that
        chunk) and reports the seed chunk so it can be excluded from
        the results. ``q=`` embeds the query string. Exactly one is
        required.
        """
        store = self.hub.store
        assert store is not None  # guarded by caller
        if like:
            from precis.handlers._link_target import parse_link_target

            tgt = parse_link_target(str(like), store=store)
            if tgt.pos is None:
                chunk_id = store.seed_chunk_for_ref(tgt.ref_id)
            else:
                block = store.get_block(tgt.ref_id, pos=tgt.pos)
                chunk_id = block.id if block is not None else None
            vec = store.get_chunk_vector(chunk_id) if chunk_id is not None else None
            if vec is None:
                raise BadInput(
                    f"like={like!r} has no embedding yet",
                    next="run `precis worker` to embed it, or seed with q=",
                )
            return vec, chunk_id, f"like={like}"

        if isinstance(q, str) and q.strip():
            embedder = getattr(self.hub, "embedder", None)
            if embedder is None:
                raise Unsupported(
                    "angle search by q= needs an embedder; "
                    "seed with like='kind:id' instead"
                )
            # Angle search is purely semantic — there's no lexical leg
            # to fall back to — so a failing embedder must surface as a
            # clean Upstream rather than a bare 500.
            try:
                return embedder.embed_one(q), None, q
            except Upstream:
                raise
            except Exception as exc:
                raise Upstream(
                    "angle search could not embed q=",
                    next="retry shortly (embedder may be warming)",
                ) from exc

        raise BadInput(
            "angle search requires q= or like=",
            next="search(q='topic', angle=0.5)  or  search(like='memory:42', angle=0)",
        )
