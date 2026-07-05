"""Oracle lenses — named sampling policies over oracle traditions.

A *lens* biases which wisdom tradition an oracle consult draws from. The
scientist / leader / artist personas now live as ordinary oracle
traditions (``scientists``, ``leadership``, ``artists``), so a lens is
just a favoured-tradition set plus a mixing rule — no bespoke dream-only
persona store.

The mixing rule is the "p-hack, made honest" the oracle already runs (see
``handlers/oracle.py`` / ``precis-oracle-help.md``): a random draw of a
provocation, here *biased* toward the favoured traditions but never
monopolised by them.

Draw policy (the ``sci`` lens is the motivating case — 50 % science):

* with probability ``bias`` (default 0.5) draw from a favoured tradition,
* with probability ``1 - bias`` draw from one of the *other* traditions,
* in each bucket pick the **tradition** uniformly, then an entry within it
  uniformly — even across *traditions*, not entries, so a 64-entry
  tradition (iching) doesn't drown out a 6-entry one (Reto's call).

Selection is CSPRNG by default (``secrets``), matching the oracle's
"consult" semantic; inject a ``random.Random`` for deterministic tests.

The output is a spark to react to, never a conclusion — verification
stays downstream (dream memories start ``tier:dream`` and only earn
``tier:synthetic-insight`` once grounded).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from precis.errors import BadInput

if TYPE_CHECKING:
    from random import Random

    from precis.store import Block, Ref, Store


# Predefined lenses: name → favoured oracle tradition slugs. Passing
# several lens names unions their favoured sets (``lens=['sci','art']``).
# Unknown names raise so a typo is caught at the boundary, not silently
# ignored into an all-traditions draw.
LENS_REGISTRY: dict[str, tuple[str, ...]] = {
    "sci": ("scientists",),
    "lead": ("leadership",),
    "art": ("artists",),
    "people": ("scientists", "leadership", "artists"),
}

#: Default favoured-vs-rest split. 0.5 = the "50 % science, 50 % the rest"
#: policy; 1.0 collapses to "favoured only", 0.0 to "everything but".
DEFAULT_BIAS = 0.5


@dataclass(frozen=True)
class LensDraw:
    """One entry drawn under a lens policy."""

    ref: Ref
    block: Block
    from_favoured: bool


def resolve_lens_traditions(lens_names: list[str]) -> set[str]:
    """Union the favoured tradition slugs for ``lens_names``.

    Raises :class:`BadInput` on any unknown lens name so the caller
    (agent surface or dream config) learns the valid set.
    """
    if not lens_names:
        raise BadInput(
            "lens= requires at least one lens name",
            options=sorted(LENS_REGISTRY),
        )
    favoured: set[str] = set()
    for name in lens_names:
        key = str(name).strip().lower()
        traditions = LENS_REGISTRY.get(key)
        if traditions is None:
            raise BadInput(
                f"unknown oracle lens {name!r}",
                options=sorted(LENS_REGISTRY),
                next="get(kind='oracle', args={'lens': ['sci']})",
            )
        favoured.update(traditions)
    return favoured


def draw_lens_entry(
    store: Store,
    lens_names: list[str],
    *,
    bias: float = DEFAULT_BIAS,
    rng: Random | None = None,
) -> LensDraw | None:
    """Draw one oracle entry under the ``lens_names`` mixture policy.

    Returns ``None`` when no oracle tradition with any entry is loaded
    (e.g. a fresh DB before ``oracle_sync`` runs) — the caller then
    proceeds unlensed rather than failing.
    """
    favoured = resolve_lens_traditions(lens_names)
    refs = store.list_refs(kind="oracle", limit=1000)
    if not refs:
        return None
    fav = [r for r in refs if (r.slug or "") in favoured]
    rest = [r for r in refs if (r.slug or "") not in favoured]

    # Coin toss picks the bucket; fall back to the other bucket when the
    # chosen one is empty (e.g. lens covers every loaded tradition, or the
    # favoured traditions aren't ingested yet).
    use_favoured = bool(fav) and (not rest or _coin(bias, rng))
    primary, fallback = (fav, rest) if use_favoured else (rest, fav)

    picked = _pick_ref_with_blocks(store, primary, rng)
    if picked is None:
        picked = _pick_ref_with_blocks(store, fallback, rng)
        use_favoured = not use_favoured
    if picked is None:
        return None

    ref, blocks = picked
    block = blocks[_below(len(blocks), rng)]
    return LensDraw(ref=ref, block=block, from_favoured=(ref.slug or "") in favoured)


def render_lens_block_from_draw(draw: LensDraw) -> str:
    """The dream's variable-layer text for a drawn entry.

    Mirrors ``dream_seed.render_lens_block`` so a persona-from-oracle
    reads identically to the legacy in-YAML lens: a ``## This cycle's
    lens`` heading the dream directive already knows how to honour.
    """
    name = _entry_title(draw.block) or (draw.ref.title or "?")
    body = (draw.block.text or "").strip()
    return f"## This cycle's lens: {name}\n\n{body}\n"


# ── internals ───────────────────────────────────────────────────────


def _pick_ref_with_blocks(
    store: Store, pool: list[Ref], rng: Random | None
) -> tuple[Ref, list[Block]] | None:
    """Pick a random ref from ``pool`` that has at least one block.

    Loads blocks lazily and drops empty traditions, retrying until the
    pool is exhausted — so an empty tradition never yields an empty draw.
    """
    remaining = list(pool)
    while remaining:
        ref = remaining.pop(_below(len(remaining), rng))
        blocks = store.list_blocks_for_ref(ref.id)
        if blocks:
            return ref, blocks
    return None


def _below(n: int, rng: Random | None) -> int:
    """Uniform int in ``[0, n)`` — CSPRNG unless an rng is injected."""
    if n <= 1:
        return 0
    return rng.randrange(n) if rng is not None else secrets.randbelow(n)


def _coin(p: float, rng: Random | None) -> bool:
    """True with probability ``p`` (clamped to [0, 1])."""
    if p <= 0.0:
        return False
    if p >= 1.0:
        return True
    r = rng.random() if rng is not None else secrets.randbelow(10**9) / 10**9
    return r < p


def _entry_title(block: Block) -> str | None:
    """The entry title from ``meta.section_path[0]`` (oracle ingest shape)."""
    meta: dict[str, Any] = block.meta or {}
    path = meta.get("section_path") or []
    if isinstance(path, list) and path:
        first = path[0]
        if isinstance(first, str) and first.strip():
            return first.strip()
    return None


__all__ = [
    "DEFAULT_BIAS",
    "LENS_REGISTRY",
    "LensDraw",
    "draw_lens_entry",
    "render_lens_block_from_draw",
    "resolve_lens_traditions",
]
