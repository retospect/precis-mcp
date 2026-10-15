"""``chem`` kind — chemical name / CAS / abbreviation → SMILES + InChIKey.

``get(kind='chem', id='aspirin')`` resolves the identifier through the
tiers in :mod:`precis.handlers._chem_resolve` (abbreviation table → OPSIN
→ PubChem, cross-checked against NCI/CIR) and caches the answer 30 days.
Every answer names its source; an ambiguous identifier is refused with
the candidate structures listed, and that refusal is cached too. A
lookup where nothing answered and a source faulted raises ``Upstream``
and is *not* cached, so an outage never pins a false "not found".

The structured answer lives in ``cache_state.meta`` (``smiles``,
``inchikey``, ``status``, ``candidates``, ``tiers``) for callers that
want it without parsing the body.
"""

from __future__ import annotations

from typing import ClassVar

from precis.errors import BadInput, Upstream
from precis.handlers._cache_base import CacheBackedHandler, FetchResult
from precis.handlers._chem_resolve import (
    Fetch,
    Resolution,
    cas_checksum_ok,
    http_fetch,
    is_cas,
    normalise_query,
    resolve,
)
from precis.protocol import KindSpec
from precis.store.types import ChunkInsert

_ATTRIBUTION = (
    "Sources: PubChem (NCBI, public domain), OPSIN (EBI, MIT), NCI/CADD "
    "Chemical Identifier Resolver, precis abbreviation table. Structures "
    "canonicalised with rdkit when available."
)


class ChemHandler(CacheBackedHandler):
    """``chem`` — resolve one chemical identifier to a structure."""

    spec: ClassVar[KindSpec] = KindSpec(
        kind="chem",
        title="Chemical identifier resolver",
        description=(
            "Resolve a chemical name, CAS number or lab abbreviation to "
            "canonical SMILES + InChIKey: abbreviation table, then OPSIN, "
            "then PubChem cross-checked against NCI/CIR. Every answer names "
            "its source; ambiguous identifiers are refused with candidates "
            "listed. Cached 30 days. See precis-chem-help."
        ),
        supports_get=True,
        supports_search=False,
        supports_tag=True,
        supports_link=True,
        is_numeric=False,
        id_required=True,
    )

    provider: ClassVar[str] = "chem"
    ttl_seconds: ClassVar[int | None] = 30 * 24 * 60 * 60
    attribution: ClassVar[str] = _ATTRIBUTION
    corpus_slug: ClassVar[str] = "default"
    example_query: ClassVar[str] = "aspirin"
    chunk_target_chars: ClassVar[int] = 0

    #: Tests swap in a recorded-fixture fetcher.
    fetch: Fetch | None = None

    def _canonical_key(self, query: str, *, literal: bool = False) -> str:
        key = normalise_query(query)
        if not key:
            raise BadInput(
                "chem requires a name, CAS number or abbreviation",
                next="get(kind='chem', id='aspirin')",
            )
        if len(key) > 300:
            raise BadInput("chem identifier longer than 300 characters")
        if is_cas(key) and not cas_checksum_ok(key):
            raise BadInput(
                f"{key!r} looks like a CAS number but fails its check digit",
                next="re-check the digits, or pass the compound name",
            )
        return key

    def _recover_key(self, ref, cache):
        return (cache.meta or {}).get("query")

    def _fetch(self, key: str) -> FetchResult:
        # CIR only corroborates, and it was unreachable from prod at r38
        # (TLS handshake timeouts): short timeout, no connect retry.
        if self.fetch is not None:
            res = resolve(key, self.fetch)
        else:
            res = resolve(key, http_fetch(), http_fetch(timeout_s=8.0, attempts=1))
        if res.status == "fault":
            raise Upstream(
                f"chem: no source answered for {key!r} ({_tier_line(res)})",
                next="retry later; the miss was not cached",
            )
        return FetchResult(
            title=_title(res),
            body_blocks=[ChunkInsert(ord=0, text=render_resolution(res))],
            cost_usd=None,
            meta=res.to_meta(),
        )


def _title(res: Resolution) -> str:
    ans = res.answer
    if ans is not None:
        return f"{res.query} → {ans.smiles}"
    if res.status == "ambiguous":
        return f"{res.query}: ambiguous ({len(res.candidates)} readings)"
    return f"{res.query}: not found"


def _tier_line(res: Resolution) -> str:
    return " · ".join(f"{t}: {o}" for t, o in res.tiers) or "none"


def render_resolution(res: Resolution) -> str:
    """The cached body. Plain lines so the agent can copy SMILES as-is."""
    lines: list[str] = []
    ans = res.answer
    if ans is not None:
        lines += [
            f"{res.query} ({res.query_type}) → resolved",
            f"smiles:   {ans.smiles}",
            f"inchikey: {ans.inchikey or '(not computed)'}",
            f"name:     {ans.name}",
            f"source:   {ans.source}"
            + (f" — {ans.source_ref}" if ans.source_ref else ""),
        ]
        if ans.source_smiles and ans.source_smiles != ans.smiles:
            lines.append(f"source smiles: {ans.source_smiles}")
    elif res.status == "ambiguous":
        if len(res.candidates) == 1:
            lines.append(
                f"{res.query} ({res.query_type}) → REFUSED: underspecified. The "
                "registry's pick is shown; confirm it by asking for its full "
                "name or CAS:"
            )
        else:
            lines.append(
                f"{res.query} ({res.query_type}) → REFUSED: ambiguous, "
                f"{len(res.candidates)} readings. Pick one by its full name or CAS:"
            )
        for i, c in enumerate(res.candidates, 1):
            ref = f" — {c.source_ref}" if c.source_ref else ""
            lines.append(
                f"  {i}. {c.name}: {c.smiles}  {c.inchikey or ''}  ({c.source}{ref})"
            )
    else:
        lines.append(
            f"{res.query} ({res.query_type}) → not found by any source. "
            "Try the IUPAC name or the CAS number."
        )
    lines.append(f"tried:    {_tier_line(res)}")
    lines.append(f"canonical: {res.canonicaliser}")
    lines += [f"note: {n}" for n in res.notes]
    return "\n".join(lines)


__all__ = ["ChemHandler", "render_resolution"]
