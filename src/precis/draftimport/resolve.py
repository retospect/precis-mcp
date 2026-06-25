"""Cite key -> precis paper slug resolution (DB-touching).

The resolution backbone for the import. For each LaTeX cite key:

1. **identifier-shaped key** — if the key itself looks like a DOI/arXiv
   (rare), ``find_paper_ref_by_identifier`` lands the ref directly.
2. **cite_key alias** — the common case: the bib key IS a precis paper's
   ``cite_key`` alias (old- or new-style). ``find_ref_by_identifier(
   'cite_key', key, kind='paper')`` matches it. (A plain key detects as no
   identifier scheme, so step 1 alone never reaches the ``cite_key`` rows —
   this step is what actually resolves ~70% of real cites, no .bib needed.)
3. **DOI / arXiv bridge** — else use the ``.bib`` entry's DOI/arXiv
   (also an identifier) to land the same ref.
4. **stub** (opt-in) — else, if the entry carries a DOI/arXiv and
   ``create_stubs`` is set, mint a stub paper (``upsert_stub_paper``,
   idempotent on the identifier) so the cite resolves and the
   ``fetch_oa`` worker pulls the PDF on a later pass.
5. **unresolved** — otherwise record a miss; the importer emits a bare
   ``[§key]`` (export degrades to a ``[missing paper]`` bib stub).

Whatever ref we land, we emit its ``cite_key`` and — durably — register
the book's own key as an additional ``cite_key`` alias on that ref, so a
second import in the book's vocabulary resolves directly next time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from precis.draftimport.tex import BibEntry


@dataclass
class KeyMap:
    slug: dict[str, str] = field(default_factory=dict)  # key -> precis cite_key
    refid: dict[str, int] = field(
        default_factory=dict
    )  # key -> paper ref_id (for [pa<id>])
    via: dict[str, str] = field(default_factory=dict)  # key -> alias|doi|arxiv|stub
    stubbed: list[str] = field(default_factory=list)  # keys minted as stubs
    unresolved: list[str] = field(default_factory=list)  # keys with no landing


def _ref_cite_key(store: Any, ref_id: int) -> str | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT id_value FROM ref_identifiers "
            "WHERE ref_id = %s AND id_kind = 'cite_key' LIMIT 1",
            (ref_id,),
        ).fetchone()
    return row[0] if row else None


def _try_identifier(store: Any, value: str | None) -> int | None:
    if not value:
        return None
    try:
        return store.find_paper_ref_by_identifier(value)
    except Exception:
        return None


def _try_cite_key(store: Any, key: str | None) -> int | None:
    """Match the bib key directly against a paper's ``cite_key`` alias.

    ``find_paper_ref_by_identifier`` auto-detects the scheme from the value
    *shape* (DOI/arXiv/S2/…), so a plain bib key like ``zhao2024PdInCo`` or
    ``smith2002`` detects as no scheme and never reaches the ``cite_key``
    rows. But precis stores every paper's cite key(s) — old- and new-style —
    as ``id_kind='cite_key'`` aliases, so the key usually IS the paper's
    handle. Look it up explicitly (case-insensitively, via the identifier
    normaliser), scoped to ``kind='paper'`` so a same-named draft slug can't
    shadow it."""
    if not key:
        return None
    try:
        return store.find_ref_by_identifier("cite_key", key, kind="paper")
    except Exception:
        return None


def resolve_key(
    store: Any,
    key: str,
    entry: BibEntry | None,
    *,
    create_stubs: bool = False,
    set_by: str = "tex-import",
    register_alias: bool = True,
) -> tuple[str | None, str, int | None]:
    """Resolve one cite key -> (precis_slug | None, via, ref_id | None)."""
    ref_id = _try_identifier(store, key)
    via = "alias"
    if ref_id is None:
        # the bib key itself is (usually) a precis paper's cite_key alias
        ref_id = _try_cite_key(store, key)
        via = "cite_key"
    if ref_id is None and entry is not None:
        ref_id = _try_identifier(store, entry.doi)
        via = "doi"
        if ref_id is None and entry.arxiv:
            ref_id = _try_identifier(store, entry.arxiv)
            via = "arxiv"
    if (
        ref_id is None
        and create_stubs
        and entry is not None
        and (entry.doi or entry.arxiv)
    ):
        ids = [("doi", entry.doi)] if entry.doi else []
        if entry.arxiv:
            ids.append(("arxiv", entry.arxiv))
        year = int(entry.year) if entry.year and entry.year.isdigit() else None
        ref_id, _ = store.upsert_stub_paper(
            identifiers=ids, title=entry.title, year=year, set_by=set_by
        )
        via = "stub"
    if ref_id is None:
        return None, "unresolved", None
    slug = _ref_cite_key(store, ref_id)
    if slug is None:
        return None, "unresolved", None
    if register_alias and key != slug:
        # additively register the book's key as another cite_key alias
        try:
            store.insert_ref_identifiers(ref_id, [("cite_key", key, set_by)])
        except Exception:
            pass
    return slug, via, ref_id


@dataclass
class GlossaryResult:
    handle: dict[str, str] = field(default_factory=dict)  # gls key -> term handle
    created: list[str] = field(default_factory=list)  # shorts newly defined
    reused: list[str] = field(default_factory=list)  # shorts already present
    conflicts: list[tuple[str, str, str]] = field(
        default_factory=list
    )  # (short, existing_long, new_long)


def import_glossary(
    store: Any,
    draft_ref_id: int,
    acronyms: dict[str, tuple[str, str]],
) -> GlossaryResult:
    """Define one ``term`` chunk per ``\\newacronym`` entry (precis abbrev
    method) and return the ``{key: term-handle}`` map plus a conflict report.

    **The `short` is the term's identity** — that is how precis's
    abbreviation model is keyed (``{SHORT: long}``), so a term in two
    surface forms (``MOF`` / ``MOFs``) is *one* term, not two: both
    ``[gls@key]`` and ``[glspl@key]`` resolve to the same handle and only
    the rendered surface differs. Plurals/variants are surfaces of the one
    short-keyed term, never separate terms.

    **Defined twice over time** is resolved by short-identity:

    * same short, same long  -> reuse the existing term's handle (no dup).
    * same short, *different* long -> a real semantic conflict (one
      abbreviation cannot expand two ways). We keep the existing
      definition and record ``(short, existing_long, new_long)`` in
      ``conflicts`` for a human to reconcile — never silently overwrite or
      duplicate.
    """
    res = GlossaryResult()
    by_short = {
        short: h for h, (short, _long) in store.draft_terms(draft_ref_id).items()
    }
    by_short_long = {
        short: long for _h, (short, long) in store.draft_terms(draft_ref_id).items()
    }
    gloss_heading = "¶" + store.ensure_glossary_heading(draft_ref_id)
    for key, (short, long) in acronyms.items():
        if short in by_short:
            res.handle[key] = by_short[short]
            res.reused.append(short)
            if (by_short_long.get(short) or "").strip() != (long or "").strip():
                res.conflicts.append((short, by_short_long.get(short, ""), long))
            continue
        chunks = store.add_chunks(
            ref_id=draft_ref_id,
            chunk_kind="term",
            text=long,
            at={"into": gloss_heading},
            meta={"short": short, "long": long},
        )
        if chunks:
            res.handle[key] = chunks[0].handle
            by_short[short] = chunks[0].handle
            by_short_long[short] = long
            res.created.append(short)
    return res


def build_keymap(
    store: Any,
    bib: dict[str, BibEntry],
    used_keys: list[str],
    *,
    create_stubs: bool = False,
) -> KeyMap:
    """Resolve every used key, minting stubs when asked. Idempotent."""
    km = KeyMap()
    for key in used_keys:
        slug, via, ref_id = resolve_key(
            store, key, bib.get(key), create_stubs=create_stubs
        )
        if slug is None:
            km.unresolved.append(key)
            continue
        km.slug[key] = slug
        if ref_id is not None:
            km.refid[key] = ref_id
        km.via[key] = via
        if via == "stub":
            km.stubbed.append(key)
    return km
