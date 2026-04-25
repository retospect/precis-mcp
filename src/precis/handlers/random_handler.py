"""RandomHandler — vector-space content sampler.

Stateless, read-only, free.  Backed by ``acatome-store`` (SQL +
pgvector).  No new tables.

Two access modes, dispatched by URI shape:

- **Uniform random** (no seed string): ``random:`` returns one ref
  uniformly at random from the corpus, optionally filtered by
  ``?corpus=`` or ``?corpora=``.
- **Blast radius** (with seed string): ``random:<seed>`` embeds the
  seed via the same encoder used by ``search_text`` and returns the
  top-K refs within an optional cosine-distance ceiling
  (``?radius=``).  Ideation skill territory.

Sampling unit (ref vs. chunk) is normally inferred from the target
corpus's ``Corpus.sample_unit`` column — set to ``chunk`` for the
oracle corpus (one chunk = one entry inside a tradition's paper),
``ref`` for everything else.  ``?from=chunks`` / ``?from=refs``
overrides explicitly.

Module name is ``random_handler`` (not ``random``) because the latter
shadows stdlib's ``random`` module.

URI surface (path is the entire opaque string after ``random:``):

| URI                                          | Behaviour                              |
|----------------------------------------------|----------------------------------------|
| ``random:``                                  | one uniform-random ref                 |
| ``random:?n=3``                              | three refs                             |
| ``random:?corpus=papers``                    | filter to one corpus                   |
| ``random:?corpora=papers,memories``          | multiple corpora                       |
| ``random:?seed=42``                          | reproducible sampling                  |
| ``random:?corpus=oracle``                    | one chunk (oracle is chunk-sampled)    |
| ``random:?corpus=oracle&tag=stoic``          | filter to stoic-tagged refs            |
| ``random:?corpus=oracle&not-tag=built-in``   | exclude built-in refs                  |
| ``random:?corpus=oracle&tag=stoic,koan``     | tag union (any-of)                     |
| ``random:?from=chunks&corpus=papers``        | force chunk sampling on a ref corpus   |
| ``random:<seed-string>``                     | blast-radius near the seed             |
| ``random:<seed>?radius=0.5``                 | blast-radius with distance ceiling     |
| ``random:/help``                             | onboarding skill inline                |

Caps: ``?n`` clamps to ``[1, 20]``; ``?radius`` clamps to
``[0.0, 1.0]``.

Tag matching: ``?tag=`` is **OR-of-listed-values** across multiple
tags (any match).  ``?not-tag=`` is **AND-of-exclusions** (no tag
in the list may be present).  Both are implemented at the SQL
layer for chunk-mode (JOIN to refs.tags) and at the corpus-fetch
layer for ref-mode.  Empty tag column treated as no tags.
"""

from __future__ import annotations

import logging
import random as _random
from typing import Any, ClassVar

from precis.protocol import ErrorCode, Handler, PrecisError

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


_MAX_N = 20
_DEFAULT_N = 1
_MAX_RADIUS = 1.0
_MIN_RADIUS = 0.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _split_query(s: str) -> tuple[str, dict[str, str]]:
    """Split ``seed-string?a=1&b=2`` into ``("seed-string", {...})``.

    Same shape as ``rng._split_query`` — copied here rather than
    imported to keep handler modules independent.  The leading-``?``
    form (``random:?corpus=oracle``) is the common agent shape, so
    we handle an empty head gracefully.
    """
    if "?" not in s:
        return s, {}
    head, qs = s.split("?", 1)
    params: dict[str, str] = {}
    for kv in qs.split("&"):
        if not kv:
            continue
        if "=" in kv:
            k, v = kv.split("=", 1)
            params[k] = v
        else:
            params[kv] = ""
    return head, params


def _parse_int(params: dict[str, str], key: str, default: int) -> int:
    """Pull an integer param with bounds-checking."""
    if key not in params:
        return default
    raw = params[key]
    try:
        return int(raw)
    except ValueError as exc:
        raise PrecisError(
            ErrorCode.PARAM_INVALID,
            cause=f"?{key}= must be an integer; got {raw!r}",
        ) from exc


def _parse_float(params: dict[str, str], key: str) -> float | None:
    if key not in params:
        return None
    raw = params[key]
    try:
        return float(raw)
    except ValueError as exc:
        raise PrecisError(
            ErrorCode.PARAM_INVALID,
            cause=f"?{key}= must be a float; got {raw!r}",
        ) from exc


def _parse_corpora(params: dict[str, str]) -> list[str]:
    """Resolve ``?corpus=`` / ``?corpora=`` into a (possibly empty) list."""
    out: list[str] = []
    if "corpus" in params:
        out.append(params["corpus"].strip())
    if "corpora" in params:
        for c in params["corpora"].split(","):
            c = c.strip()
            if c:
                out.append(c)
    return [c for c in out if c]


def _parse_tag_filter(params: dict[str, str], *keys: str) -> list[str]:
    """Pull a comma-separated tag list from any of the given param keys.

    Accepts ``?tag=stoic``, ``?tag=stoic,koan``, ``?tags=stoic,koan``,
    or repeated ``?tag=stoic&tag=koan`` (the ``_split_query`` parser
    keeps only the last for repeats; comma is the canonical form).
    """
    out: list[str] = []
    for k in keys:
        if k in params and params[k].strip():
            for v in params[k].split(","):
                v = v.strip()
                if v:
                    out.append(v)
    # Preserve order, drop dups.
    seen: set[str] = set()
    deduped: list[str] = []
    for t in out:
        if t not in seen:
            seen.add(t)
            deduped.append(t)
    return deduped


def _ref_has_any_tag(ref, wanted: list[str]) -> bool:
    """True iff the ref carries at least one of the wanted tags."""
    import json as _json
    raw = getattr(ref, "tags", None) or "[]"
    try:
        tags = _json.loads(raw) if isinstance(raw, str) else list(raw)
    except (TypeError, ValueError):
        tags = []
    return any(t in tags for t in wanted)


def _ref_has_no_tag(ref, banned: list[str]) -> bool:
    """True iff the ref carries none of the banned tags."""
    import json as _json
    raw = getattr(ref, "tags", None) or "[]"
    try:
        tags = _json.loads(raw) if isinstance(raw, str) else list(raw)
    except (TypeError, ValueError):
        tags = []
    return not any(t in tags for t in banned)


def _resolve_sample_unit(
    corpora: list[str], params: dict[str, str], store,
) -> str:
    """Decide whether to sample at the ``ref`` or ``chunk`` level.

    Precedence:
      1. Explicit ``?from=`` query parameter (``refs`` / ``chunks``).
      2. Single-corpus implicit lookup of ``Corpus.sample_unit``.
      3. Default ``"ref"``.

    Multi-corpus queries with mixed sample_unit values fall back to
    ``ref`` and surface a hint at render time — there is no clean way
    to mix chunk-mode with ref-mode in a single uniform sample
    without making the result confusing.
    """
    explicit = (params.get("from") or "").strip().lower()
    if explicit in {"chunks", "blocks"}:
        return "chunk"
    if explicit in {"refs", "ref"}:
        return "ref"
    if explicit:
        raise PrecisError(
            ErrorCode.PARAM_INVALID,
            cause=f"?from={explicit!r} unrecognised",
            next="valid: ?from=chunks or ?from=refs",
        )
    if len(corpora) == 1:
        try:
            from acatome_store.models import Corpus
            with store._Session() as session:
                row = session.get(Corpus, corpora[0])
            if row is not None:
                return getattr(row, "sample_unit", None) or "ref"
        except (ImportError, AttributeError):
            pass
    return "ref"


def _clamp_n(n: int) -> int:
    if n < 1:
        raise PrecisError(
            ErrorCode.PARAM_INVALID,
            cause=f"?n={n} must be ≥ 1",
        )
    if n > _MAX_N:
        raise PrecisError(
            ErrorCode.PARAM_INVALID,
            cause=f"?n={n} exceeds cap {_MAX_N}",
        )
    return n


def _clamp_radius(r: float) -> float:
    if r < _MIN_RADIUS or r > _MAX_RADIUS:
        raise PrecisError(
            ErrorCode.PARAM_INVALID,
            cause=f"?radius={r} outside [{_MIN_RADIUS}, {_MAX_RADIUS}]",
        )
    return r


# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------


def _footer(seed: int | None, mode: str, n: int, corpora: list[str]) -> str:
    seed_str = str(seed) if seed is not None else "os"
    corpora_str = ",".join(corpora) if corpora else "all"
    return (
        f"\n\n---\n"
        f"_Sampled by precis · mode={mode} · seed={seed_str} · "
        f"n={n} · corpora=[{corpora_str}]_"
    )


def _filter_summary(wanted_tags: list[str], banned_tags: list[str]) -> str:
    """Render a compact ``(tag=…, not-tag=…)`` parenthetical."""
    parts: list[str] = []
    if wanted_tags:
        parts.append(f"tag={','.join(wanted_tags)}")
    if banned_tags:
        parts.append(f"not-tag={','.join(banned_tags)}")
    return f"  ({'; '.join(parts)})" if parts else ""


def _no_hits_msg(
    corpora: list[str], wanted_tags: list[str], banned_tags: list[str],
    *, unit: str,
) -> str:
    """Helpful "no hits" message that surfaces every applied filter."""
    bits: list[str] = []
    if corpora:
        bits.append(f"corpora=[{','.join(corpora)}]")
    if wanted_tags:
        bits.append(f"tag=[{','.join(wanted_tags)}]")
    if banned_tags:
        bits.append(f"not-tag=[{','.join(banned_tags)}]")
    constraint = ", ".join(bits) if bits else "no constraints"
    return (
        f"random pick: no {unit}s match {constraint}\n\n"
        f"Hints:\n"
        f"  - is the corpus ingested?  For oracle, run\n"
        f"    `precis-ingest-oracle`.  For memories,\n"
        f"    `get(id='memory:/recent')` confirms population.\n"
        f"  - relax the filters: drop `?tag=` or `?not-tag=`,\n"
        f"    or widen `?corpus=` / `?corpora=`."
    )


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class RandomHandler(Handler):
    """Handler for the ``random:`` scheme — vector-space sampler.

    Agent usage::

        get(id='random:')                        — one random ref
        get(id='random:?n=3')                    — three refs
        get(id='random:?corpus=papers')          — filter
        get(id='random:?corpus=oracle&n=3')      — three oracle chunks
        get(id='random:?corpus=oracle&tag=stoic')— stoic tradition only
        get(id='random:cascading failure?n=3')   — blast-radius
        get(id='random:?seed=42')                — reproducible
    """

    scheme = "random"
    writable = False
    views: ClassVar[set[str]] = {"help"}
    onboarding_skill = "random-basics"

    def read(
        self,
        path: str,
        selector: str | None,
        view: str | None,
        subview: str | None,
        query: str,
        summarize: bool,
        depth: int,
        page: int,
        **kwargs: Any,
    ) -> str:
        # ``search(type='random', query='X', top_k=5)`` flows through
        # ``read()`` carrying a ``top_k`` kwarg; treat it as a request
        # for ``?n=`` of that size.  When the caller already pinned
        # ``?n=`` in the URI (i.e. an explicit override), the URI value
        # wins.  ``query`` from search() promotes to the seed string
        # if no seed was already in the path.
        top_k_kw = kwargs.get("top_k")
        raw = (path or "").strip()
        body, params = _split_query(raw)

        if top_k_kw and "n" not in params:
            params["n"] = str(top_k_kw)

        # Help view (accept ``/help`` and ``help``).
        if body in {"/help", "help"}:
            return self._help()

        # Strip a single leading slash so ``random:/`` reads as empty body.
        seed_string = body.lstrip("/")
        # ``search(type='random', query='X')`` — query promotes to seed.
        if not seed_string and query.strip():
            seed_string = query.strip()

        seed = _parse_int(params, "seed", -1)
        seed_val: int | None = seed if seed >= 0 else None
        n = _clamp_n(_parse_int(params, "n", _DEFAULT_N))
        corpora = _parse_corpora(params)
        radius_raw = _parse_float(params, "radius")
        radius = _clamp_radius(radius_raw) if radius_raw is not None else None

        if seed_string:
            return self._blast_radius(
                seed_string=seed_string,
                n=n,
                corpora=corpora,
                radius=radius,
                seed=seed_val,
            )
        return self._uniform(
            n=n, corpora=corpora, seed=seed_val, params=params,
        )

    # ------------------------------------------------------------------
    # Uniform mode
    # ------------------------------------------------------------------

    def _uniform(
        self,
        *,
        n: int,
        corpora: list[str],
        seed: int | None,
        params: dict[str, str],
    ) -> str:
        from precis._store import get_store

        store = get_store()
        rng = _random.Random(seed) if seed is not None else _random.Random()

        # Resolve sampling unit (ref vs. chunk).
        sample_unit = _resolve_sample_unit(corpora, params, store)

        # Tag filters — applied in Python after corpus fetch (works for
        # both ref and chunk modes; cheap at oracle/wisdom scale).
        wanted_tags = _parse_tag_filter(params, "tag", "tags", "tradition")
        banned_tags = _parse_tag_filter(params, "not-tag", "not-tags", "exclude")

        if sample_unit == "chunk":
            return self._uniform_chunks(
                n=n, corpora=corpora, seed=seed,
                wanted_tags=wanted_tags, banned_tags=banned_tags, rng=rng,
            )
        return self._uniform_refs(
            n=n, corpora=corpora, seed=seed,
            wanted_tags=wanted_tags, banned_tags=banned_tags, rng=rng,
        )

    def _uniform_refs(
        self,
        *,
        n: int,
        corpora: list[str],
        seed: int | None,
        wanted_tags: list[str],
        banned_tags: list[str],
        rng,
    ) -> str:
        from acatome_store.models import Ref

        from precis._store import get_store

        store = get_store()
        with store._Session() as session:
            qry = session.query(Ref)
            if corpora:
                if len(corpora) == 1:
                    qry = qry.filter(Ref.corpus_id == corpora[0])
                else:
                    qry = qry.filter(Ref.corpus_id.in_(corpora))
            # Pool cap: 200 × n + tag filter pressure.  When tags
            # filter aggressively, we may need a larger pool — bump
            # by 10× when filters are present.
            tag_pressure = 10 if (wanted_tags or banned_tags) else 1
            pool_size = max(n * 200 * tag_pressure, 100)
            pool = qry.limit(pool_size).all()

        # Apply tag filters in Python.
        if wanted_tags:
            pool = [r for r in pool if _ref_has_any_tag(r, wanted_tags)]
        if banned_tags:
            pool = [r for r in pool if _ref_has_no_tag(r, banned_tags)]

        if not pool:
            return _no_hits_msg(
                corpora, wanted_tags, banned_tags, unit="ref",
            )
        picks = rng.sample(pool, min(n, len(pool)))
        body = self._render_uniform_refs(
            picks, wanted_tags=wanted_tags, banned_tags=banned_tags,
        )
        return body + _footer(seed, "uniform-ref", n, corpora)

    def _uniform_chunks(
        self,
        *,
        n: int,
        corpora: list[str],
        seed: int | None,
        wanted_tags: list[str],
        banned_tags: list[str],
        rng,
    ) -> str:
        """Sample one (or more) chunks from the corpus pool.

        Pool query: blocks JOIN refs, restrict by ``Ref.corpus_id``
        and tag filters, then random.sample in Python.  The corpus
        layer's pgvector tables don't include a "random row" SQL
        operator, so we cap the pool to a generous-but-bounded size
        (5000 chunks × tag-pressure multiplier) and sample.  At oracle
        scale (~150-300 chunks per corpus, low-thousands across
        traditions) this is exhaustive; at paper scale (millions of
        chunks) it returns a uniform sample over a bounded slice.
        """
        from acatome_store.models import Block, Ref

        from precis._store import get_store

        store = get_store()
        tag_pressure = 10 if (wanted_tags or banned_tags) else 1
        pool_size = max(n * 200 * tag_pressure, 500)
        pool_size = min(pool_size, 50000)  # hard cap

        with store._Session() as session:
            qry = (
                session.query(Block, Ref)
                .join(Ref, Block.ref_id == Ref.id)
                .filter(Block.profile == "default")
            )
            if corpora:
                if len(corpora) == 1:
                    qry = qry.filter(Ref.corpus_id == corpora[0])
                else:
                    qry = qry.filter(Ref.corpus_id.in_(corpora))
            rows = qry.limit(pool_size).all()

        # Apply tag filter in Python (Ref objects are eager-loaded
        # by the join above).
        pool: list[tuple[Any, Any]] = list(rows)
        if wanted_tags:
            pool = [(b, r) for b, r in pool if _ref_has_any_tag(r, wanted_tags)]
        if banned_tags:
            pool = [(b, r) for b, r in pool if _ref_has_no_tag(r, banned_tags)]

        if not pool:
            return _no_hits_msg(
                corpora, wanted_tags, banned_tags, unit="chunk",
            )
        picks = rng.sample(pool, min(n, len(pool)))
        body = self._render_uniform_chunks(
            picks, wanted_tags=wanted_tags, banned_tags=banned_tags,
        )
        return body + _footer(seed, "uniform-chunk", n, corpora)

    def _render_uniform_refs(
        self, refs: list, *, wanted_tags: list[str], banned_tags: list[str],
    ) -> str:
        lines: list[str] = []
        filt = _filter_summary(wanted_tags, banned_tags)
        lines.append(f"🎲 random pick · {len(refs)} ref(s){filt}")
        lines.append("")
        for r in refs:
            corpus = r.corpus_id or "?"
            title = r.title or "(no title)"
            lines.append(f"📚 {corpus}: {r.slug}")
            lines.append(f"   {title}")
        return "\n".join(lines)

    def _render_uniform_chunks(
        self, picks: list, *, wanted_tags: list[str], banned_tags: list[str],
    ) -> str:
        lines: list[str] = []
        filt = _filter_summary(wanted_tags, banned_tags)
        lines.append(f"🎲 random pick · {len(picks)} chunk(s){filt}")
        lines.append("")
        for block, ref in picks:
            slug = ref.slug or "?"
            title = ref.title or "(no title)"
            text = (block.text or "").strip()
            preview = text if len(text) <= 400 else text[:400] + "…"
            block_idx = getattr(block, "block_index", "?")
            section_path = getattr(block, "section_path", "")
            section_hint = ""
            if section_path:
                try:
                    import json as _json
                    sp = _json.loads(section_path)
                    if sp:
                        section_hint = " — " + " / ".join(str(s) for s in sp)
                except (TypeError, ValueError):
                    pass
            lines.append(f"📜 {slug}›{block_idx}{section_hint}")
            if title and title != preview[:80]:
                lines.append(f"   ({title})")
            lines.append("")
            for ln in preview.split("\n"):
                lines.append(f"   {ln}")
            lines.append("")
        return "\n".join(lines).rstrip()

    # ------------------------------------------------------------------
    # Blast-radius mode
    # ------------------------------------------------------------------

    def _blast_radius(
        self,
        *,
        seed_string: str,
        n: int,
        corpora: list[str],
        radius: float | None,
        seed: int | None,
    ) -> str:
        # ``seed`` (?seed=) is reserved here for future tie-breaking
        # of equidistant hits.  pgvector's ORDER BY cosine-distance is
        # already deterministic, so v1 ignores this value in blast mode.
        _ = seed

        from precis._store import get_store

        store = get_store()
        where: dict[str, Any] | None = None
        if corpora:
            where = {
                "corpus_id": corpora[0] if len(corpora) == 1 else {"$in": corpora}
            }

        try:
            hits = store.index.search_text(
                seed_string,
                top_k=n,
                where=where,
                max_distance=radius,
            )
        except (ImportError, ModuleNotFoundError) as exc:
            raise PrecisError(
                ErrorCode.KIND_UNAVAILABLE,
                cause=f"semantic search unavailable: {exc}",
                next="install sentence-transformers or check embedding backend",
            ) from exc

        if not hits:
            radius_hint = f" within radius {radius}" if radius is not None else ""
            corpora_hint = f" in {','.join(corpora)}" if corpora else ""
            return (
                f"blast-radius pick for {seed_string!r}{radius_hint}"
                f"{corpora_hint}: no hits\n\n"
                "Try: broader query, larger radius, more corpora."
            )

        body = self._render_blast(seed_string, hits, radius)
        return body + _footer(None, "blast-radius", n, corpora)

    def _render_blast(
        self, seed_string: str, hits: list, radius: float | None,
    ) -> str:
        radius_str = f"≤ {radius}" if radius is not None else "no cap"
        lines = [
            f"💥 blast-radius near {seed_string!r} "
            f"(cosine {radius_str}, {len(hits)} hits)",
            "",
        ]
        # Group by corpus for readability.
        buckets: dict[str, list] = {}
        for h in hits:
            cid = h.get("metadata", {}).get("corpus_id") or "_other"
            buckets.setdefault(cid, []).append(h)
        for corpus, items in buckets.items():
            lines.append(f"{corpus}:")
            for h in items:
                meta = h.get("metadata", {})
                slug = meta.get("slug", "?")
                title = meta.get("ref_title", "(no title)")
                distance = h.get("distance", 0.0)
                lines.append(f"  {distance:.3f}  {slug} — {title}")
            lines.append("")
        return "\n".join(lines).rstrip()

    # ------------------------------------------------------------------
    # Help
    # ------------------------------------------------------------------

    def _help(self) -> str:
        return (
            "# random — vector-space sampler\n\n"
            "Read-only, free.  Two modes: uniform pick from a corpus,\n"
            "or blast-radius pick semantically near a seed string.\n\n"
            "## Uniform pick\n\n"
            "- `get(id='random:')`                     — one ref, any corpus\n"
            "- `get(id='random:?n=3')`                 — three refs\n"
            "- `get(id='random:?corpus=papers')`       — filter\n"
            "- `get(id='random:?corpora=papers,memories')` — multi-corpus\n"
            "- `get(id='random:?corpus=oracle&n=3')`   — three oracle chunks\n"
            "- `get(id='random:?corpus=oracle&tag=stoic')` — stoic tradition\n"
            "- `get(id='random:?corpus=oracle&not-tag=built-in')` — personal only\n"
            "- `get(id='random:?seed=42')`             — reproducible\n\n"
            "## Blast radius (ideation)\n\n"
            "- `get(id='random:my problem?n=5')`\n"
            "- `get(id='random:cascading failure?corpus=oracle')`\n"
            "- `get(id='random:refactor?radius=0.4&n=3')`\n"
            "- `get(id='random:cross-pollinate?corpora=papers,memories')`\n\n"
            "## Knobs\n\n"
            "- `?n=<int>`       — results (1..20; default 1)\n"
            "- `?corpus=<id>`   — single-corpus filter\n"
            "- `?corpora=<a,b>` — multi-corpus\n"
            "- `?radius=<0..1>` — cosine distance ceiling (blast only)\n"
            "- `?seed=<int>`    — reproducible (uniform only)\n\n"
            "## Distinct from\n\n"
            "- `rng:` — raw random numbers (no content, no store)\n"
            "- `search()` — deterministic top-K over a query\n"
            "- `random:` — sampled / filtered content, meant to surprise\n"
        )
